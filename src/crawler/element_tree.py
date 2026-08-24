"""Depth-first element-tree walker: the MenuTree deliverable.

Model
-----
Every labelled element on a screen becomes a row. If pressing an element
reveals a different screen, its elements become rows one depth deeper. The
output is therefore a tree of *elements*, matching the expected workbook
(`1 Depth`, `2 Depth`, … `18 Depth`), not a graph of screens.

Navigation
----------
BACK-first, replay as fallback. Replay-from-launch was measured at **94%
drift** on the Samsung camera even with `pm clear` between paths: the app
restores its last-used mode, so no two launches agree and nearly every branch
was discarded. BACK is both far cheaper (no ~30s relaunch per item) and far
more reliable here, because it stays inside one app session.

After every descent we press BACK and *verify* we returned to the screen we
left. Only if that fails do we pay for a relaunch-and-replay. This keeps the
common case fast without giving up correctness.

Descend vs select
-----------------
Clicking a filter or a resolution stays on the same screen with the same
items; clicking Settings replaces them. `screen_similarity` distinguishes the
two, so options are recorded as leaves rather than recursed into.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .action_guard import DEFAULT_PRESETS, ActionGuard
from .device_driver import DeviceDriver, DriverError, make_driver
from .elements import (
    CHROME_PACKAGES,
    Element,
    enumerate_elements,
    screen_similarity,
)
from .hierarchy import (
    EMPTY_STATE,
    center_of,
    looks_like_dialog,
    parse_hierarchy,
    state_key,
)

logger = logging.getLogger(__name__)

# Keypad keys: digits, star, hash, plus. Enumerated as rows, never pressed.
#
# Pressing them is data entry, not menu navigation, and it is unsafe: the
# walker typed dialpad keys until Android auto-executed a USSD/MMI code and
# left the app sitting in com.android.phone ("USSD code running..."). Some
# vendor MMI codes are destructive -- *2767*3855# is a factory reset on
# Samsung handsets. It also produced a screen BACK cannot return to, which
# is what made navigation look broken for three debugging rounds.
KEYPAD_KEY = re.compile(r"^[0-9*#+]{1,3}$")

# System dialogs that interrupt a walk. An ANR or crash is a real defect the
# run has surfaced, so it is recorded as an incident rather than silently
# dismissed -- "the crawl caused 3 ANRs" is a finding a gate should report.
SYSTEM_DIALOGS = (
    ("anr", ("isn't responding", "is not responding")),
    ("crash", ("has stopped", "keeps stopping", "unfortunately")),
    ("ussd", ("ussd code running", "mmi code")),
)
# Preferred dismissal, in order: keep the app alive where possible.
_DISMISS_LABELS = ("Wait", "OK", "Close app", "Cancel", "Dismiss")


@dataclass
class TreeNode:
    """One row of the MenuTree."""
    label: str          # annotated, for the sheet: "Photo format [Title]"
    kind: str
    depth: int
    path: List[str] = field(default_factory=list)
    raw_label: str = ""   # the on-screen text, usable as a selector
    interactive: bool = False
    descended: bool = False
    blocked: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "raw_label": self.raw_label or self.label,
            "kind": self.kind,
            "depth": self.depth,
            "path": self.path,
            "interactive": self.interactive,
            "descended": self.descended,
            "blocked": self.blocked,
            "note": self.note,
        }


class ElementTreeWalker:
    def __init__(self, package: str, serial: Optional[str], config: dict):
        self.package = package
        self.serial = serial

        self.output_dir = Path(config.get("output_dir", "./tree_out"))
        self.max_depth = int(config.get("max_depth", 18))
        self.time_budget = float(config.get("time_budget", 3600))
        self.settle = float(config.get("settle_seconds", 1.0))
        self.ready_timeout = float(config.get("ready_timeout", 25.0))
        self.stable_interval = float(config.get("stable_interval", 0.4))
        self.state_mode = config.get("state_key_mode", "affordance")
        self.backend = config.get("driver", "auto")
        self.include_static_text = config.get("include_static_text", True)
        self.include_foreign = config.get("include_foreign", True)
        self.similarity_threshold = float(config.get("similarity_threshold", 0.6))
        self.back_attempts = int(config.get("back_attempts", 4))
        self.return_similarity = float(config.get("return_similarity", 0.75))
        self.checkpoint_every = int(config.get("checkpoint_every", 25))
        # Off by default: pm clear resets the app's saved preferences, which
        # is unacceptable on a personal device. On a disposable test device it
        # is what makes a one-shot dialog ("Turn on Location tags? Cancel /
        # Turn on") reappear so the sibling not taken first can still be
        # reached -- without it that branch is lost the moment the first
        # option is clicked, with no way back.
        self.clear_between_paths = bool(config.get("clear_between_paths", False))

        self.guard = ActionGuard.from_config(
            enabled=config.get("guard_enabled", True),
            presets=config.get("guard_presets", DEFAULT_PRESETS),
            extra=config.get("guard_extra_patterns") or [],
        )

        self.driver: Optional[DeviceDriver] = None
        self.rows: List[TreeNode] = []
        self._visited_screens: Set[str] = set()
        self._started = 0.0
        self._clicks = 0
        self._descents = 0
        self._back_ok = 0
        self._back_failed = 0
        self._relaunches = 0
        self._left_app = 0
        self._exclude = list(CHROME_PACKAGES)
        self._lost = 0
        self._dialog_recoveries = 0
        self._foreign_skipped = 0
        self._back_trace: List[Dict] = []
        self._keypad_skipped = 0
        self._incidents: List[Dict] = []
        self._reclicks = 0

    # -- capture ---------------------------------------------------------
    def _capture(self) -> Tuple[Optional[str], List[Dict], str]:
        assert self.driver is not None
        views = parse_hierarchy(self.driver.dump_hierarchy())
        if not views:
            return None, [], ""
        current = self.driver.current_package() or ""
        return state_key(views, self.state_mode, self.package), views, current

    def _await_stable(self) -> Tuple[Optional[str], List[Dict], str]:
        deadline = time.time() + self.ready_timeout
        previous, views, current = self._capture()
        settled = None
        while time.time() < deadline:
            if previous and previous != EMPTY_STATE and settled == previous:
                return previous, views, current
            settled = previous
            time.sleep(self.stable_interval)
            previous, views, current = self._capture()
        return previous, views, current

    def _elements(self, views: Sequence[Dict]) -> List[Element]:
        return enumerate_elements(
            views,
            self.package,
            include_static_text=self.include_static_text,
            include_foreign=self.include_foreign,
            exclude_packages=tuple(self._exclude),
        )

    # -- navigation ------------------------------------------------------
    def _is_same_screen(
        self, views: Sequence[Dict], target: Sequence[Element]
    ) -> bool:
        """Are we back on the screen we left?

        Compared by element overlap, not by state key. The key includes
        affordance text, and a screen can legitimately change between visits
        without becoming a different screen -- the dialer's call log carries
        timestamps inside clickable rows, so an exact-key test scored 11 of 19
        successful BACKs as failures and paid for a relaunch each time.
        """
        return screen_similarity(target, self._elements(views)) >= self.return_similarity

    def _return_to(
        self, target_key: str, path: List[str],
        target_elements: Optional[Sequence[Element]] = None,
    ) -> bool:
        """Get back to the screen we descended from.

        BACK first, because it is cheap and keeps the session. Relaunch and
        replay only when BACK fails to land us where we started.
        """
        assert self.driver is not None
        for attempt in range(1, self.back_attempts + 1):
            try:
                self.driver.press_back()
            except DriverError:
                break
            key, views, current = self._await_stable()
            landed = self._elements(views) if views else []
            dialog = self._detect_system_dialog(landed)
            if dialog:
                self._dismiss_system_dialog(landed, views, dialog)
                continue
            overlap = (
                screen_similarity(target_elements, landed)
                if target_elements is not None else -1.0
            )
            if key == target_key or (
                target_elements is not None
                and views
                and overlap >= self.return_similarity
            ):
                self._back_ok += 1
                return True

            # Instrumented: guessing at this twice was wasted effort. Record
            # what BACK actually landed on so the failure mode is readable.
            self._back_trace.append({
                "attempt": attempt,
                "depth": len(path),
                "expected": target_key[:10] if target_key else None,
                "got": key[:10] if key else None,
                "overlap": round(overlap, 3),
                "package": current,
                "landed_labels": [e.label for e in landed[:6]],
                "expected_labels": [e.label for e in (target_elements or [])[:6]],
            })
            if current and current != self.package:
                # BACK walked us out of the app entirely.
                self._left_app += 1
                break

            # Still in the app but on the wrong screen. Pressing BACK again
            # walks further away -- from the Keypad tab it reaches the call
            # log, then the launcher. Try re-entering by tapping instead,
            # while we are still inside the app.
            if self._reclick_back(target_key, path, target_elements):
                self._back_ok += 1
                return True

        self._back_failed += 1

        return self._relaunch_and_replay(target_key, path)

    def _reclick_back(
        self, target_key: str, path: List[str],
        target_elements: Optional[Sequence[Element]],
    ) -> bool:
        """Re-enter the screen by tapping the element we descended through.

        BACK is the wrong verb for tab-based navigation: leaving the Keypad
        tab lands on the call log, and BACK again exits the app. Tabs are
        re-entered by tapping. Tried while still inside the app, before
        paying for a relaunch (~30s; eight of them consumed most of one run).
        """
        if not path or self.driver is None:
            return False
        key, views, current = self._await_stable()
        if current != self.package or not views:
            return False
        live = next(
            (e for e in self._elements(views)
             if e.label == path[-1] and e.interactive),
            None,
        )
        if live is None or not self._click(live, views):
            return False
        key, views, _ = self._await_stable()
        if key == target_key or (
            target_elements is not None and views
            and self._is_same_screen(views, target_elements)
        ):
            self._reclicks += 1
            return True
        return False

    def _relaunch_and_replay(
        self, target_key: str, path: List[str], clear: bool = False
    ) -> bool:
        """Fallback: restart the app and re-walk the labels that got us here."""
        assert self.driver is not None
        self._relaunches += 1
        try:
            self.driver.start_app(self.package, clear=clear)
        except DriverError as exc:
            logger.warning("Relaunch failed: %s", exc)
            return False

        key, views, _ = self._await_stable()
        for label in path:
            element = next(
                (e for e in self._elements(views)
                 if e.label == label and e.interactive),
                None,
            )
            if element is None:
                return False
            if not self._click(element, views):
                return False
            key, views, _ = self._await_stable()
        return key == target_key

    def _detect_system_dialog(self, elements: Sequence[Element]) -> Optional[str]:
        blob = " ".join(e.label.lower() for e in elements)
        for kind, needles in SYSTEM_DIALOGS:
            if any(n in blob for n in needles):
                return kind
        return None

    def _dismiss_system_dialog(self, elements: Sequence[Element],
                               views: Sequence[Dict], kind: str) -> bool:
        """Clear an ANR/crash/USSD dialog so the walk can continue."""
        self._incidents.append({"kind": kind,
                                "labels": [e.label for e in elements[:6]]})
        logger.warning(
            "%s dialog encountered: %s -- recorded as an incident",
            kind.upper(), [e.label for e in elements[:4]],
        )
        for wanted in _DISMISS_LABELS:
            for element in elements:
                if element.label.strip().lower() == wanted.lower():
                    if self._click(element, views):
                        time.sleep(self.settle)
                        return True
        try:
            self.driver.press_back()  # type: ignore[union-attr]
            return True
        except DriverError:
            return False

    def _click(self, element: Element, views: Sequence[Dict]) -> bool:
        assert self.driver is not None
        if element.view_index >= len(views):
            return False
        centre = center_of(views[element.view_index])
        if centre is None:
            return False
        try:
            self.driver.tap(*centre)
        except DriverError:
            return False
        self._clicks += 1
        return True

    # -- walk ------------------------------------------------------------
    def _budget_left(self) -> bool:
        return (time.time() - self._started) < self.time_budget

    def _walk(self, depth: int, path: List[str]) -> None:
        if depth > self.max_depth or not self._budget_left():
            return

        screen_key, views, current = self._await_stable()
        if not screen_key or screen_key == EMPTY_STATE:
            return
        if screen_key in self._visited_screens:
            return

        if current and current != self.package:
            # state_key no longer collapses a foreign-owned screen to
            # EMPTY_STATE (a runtime permission prompt is entirely owned by
            # com.google.android.permissioncontroller and is real, addressable
            # content). But that means every foreign screen is now a
            # candidate to walk, including ones that are NOT a pop-up over
            # the app -- a browser opened via "Learn more", the launcher
            # mid-transition, an unrelated app entirely. Only descend when it
            # structurally looks like a dialog; otherwise this screen was
            # already recorded as a "leaves app" note on the row that led
            # here, and walking it further would enumerate a whole other
            # app's menu as if it were this app's.
            if not looks_like_dialog(views, self.package):
                self._foreign_skipped += 1
                logger.info(
                    "depth %-2d  not a dialog, foreign package %s -- not walked",
                    depth, current,
                )
                return
            logger.info("depth %-2d  system dialog over the app (%s)",
                        depth, current)

        self._visited_screens.add(screen_key)

        elements = self._elements(views)
        logger.info(
            "depth %-2d  %-45s  %d element(s)",
            depth, " > ".join(path[-2:]) or "<root>", len(elements),
        )

        for element in elements:
            if not self._budget_left():
                return

            blocked = self.guard.blocks("text", element.label) if element.interactive else None
            row = TreeNode(
                label=element.annotated(),
                raw_label=element.label,
                kind=element.kind,
                depth=depth,
                path=list(path),
                interactive=element.interactive,
                blocked=blocked,
            )
            self.rows.append(row)
            if len(self.rows) % self.checkpoint_every == 0:
                self._checkpoint()

            if KEYPAD_KEY.fullmatch(element.label.strip()):
                row.note = "keypad key -- recorded, not pressed"
                self._keypad_skipped += 1
                continue

            if not element.interactive or blocked or element.kind == "back":
                continue

            # Re-read the screen: an earlier click may have shifted indices.
            #
            # Compare by element overlap, never by exact key. A screen's key
            # drifts on its own -- the dialer shows call timestamps inside
            # clickable rows, so affordance text changes while the screen does
            # not. An exact test concluded we had left a screen we were still
            # on, pressed BACK, and navigated away from it: 14 relaunches from
            # 38 clicks, and the walk never got past depth 3.
            current_key, current_views, _ = self._await_stable()
            if not current_views or not self._is_same_screen(current_views, elements):
                if not self._return_to(screen_key, path, elements):
                    # Could not get back. Abandoning the whole screen would
                    # discard every sibling still unvisited, so skip just this
                    # element and re-check on the next one.
                    #
                    # The row for this element was already appended above
                    # with no note, which is indistinguishable in the sheet
                    # from a genuinely-tested leaf with nothing behind it --
                    # a real transparency gap for a coverage deliverable.
                    # Tag it so "recorded" and "actually clicked" are never
                    # confused when reading the workbook.
                    self._lost += 1
                    row.note = "NOT TESTED -- could not return to parent screen"
                    logger.warning(
                        "Could not return to depth %d screen; skipping %r",
                        depth, element.label,
                    )
                    continue
                current_key, current_views, _ = self._await_stable()
            live = next(
                (e for e in self._elements(current_views)
                 if e.label == element.label and e.interactive),
                None,
            )
            if live is None and self.clear_between_paths:
                # The element that was on this screen a moment ago is gone.
                # The likely cause is a one-shot dialog: an earlier sibling
                # (e.g. "Cancel") already dismissed it, so "Turn on" no
                # longer exists to click. Clearing and replaying the path
                # restores the app to first-run state, which makes the
                # dialog reappear so this branch is not silently lost.
                if self._relaunch_and_replay(screen_key, path, clear=True):
                    _, current_views, _ = self._await_stable()
                    live = next(
                        (e for e in self._elements(current_views)
                         if e.label == element.label and e.interactive),
                        None,
                    )
                    if live is not None:
                        self._dialog_recoveries += 1
                        row.note = "recovered via clear+relaunch (one-shot dialog)"
            if live is None or not self._click(live, current_views):
                if live is None:
                    row.note = row.note or "element vanished before click (one-shot?)"
                continue

            after_key, after_views, after_pkg = self._await_stable()
            after_probe = self._elements(after_views) if after_views else []
            dialog = self._detect_system_dialog(after_probe)
            if dialog:
                self._dismiss_system_dialog(after_probe, after_views, dialog)
                self._return_to(screen_key, path, elements)
                continue
            if not after_key or after_key == EMPTY_STATE:
                self._return_to(screen_key, path, elements)
                continue

            after_elements = self._elements(after_views)
            similarity = screen_similarity(elements, after_elements)

            if similarity < self.similarity_threshold:
                row.descended = True
                self._descents += 1
                if after_pkg and after_pkg != self.package:
                    row.note = f"leaves app -> {after_pkg}"
                self._walk(depth + 1, path + [element.label])
                self._return_to(screen_key, path, elements)
            elif after_key != screen_key:
                # Same items, different key: a selection or toggle, not a menu
                # -- choosing a filter or flipping a switch.
                row.note = "selection"
                self._return_to(screen_key, path, elements)

    # -- public ----------------------------------------------------------
    def walk(self) -> List[TreeNode]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.driver = make_driver(self.serial, self.backend, self.settle)
        ime = self.driver.current_ime_package()
        if ime and ime not in self._exclude:
            self._exclude.append(ime)
            logger.info("Excluding active keyboard package: %s", ime)
        self._started = time.time()

        self.driver.start_app(self.package, clear=False)
        self.rows.append(TreeNode(label=self.package, raw_label=self.package,
                                  kind="root", depth=1, path=[]))
        self._walk(2, [])

        logger.info("Walk finished: %s", self.stats())
        return self.rows

    def stats(self) -> Dict:
        by_depth: Dict[int, int] = {}
        for row in self.rows:
            by_depth[row.depth] = by_depth.get(row.depth, 0) + 1
        return {
            "rows": len(self.rows),
            "max_depth": max((r.depth for r in self.rows), default=0),
            "rows_by_depth": dict(sorted(by_depth.items())),
            "screens_visited": len(self._visited_screens),
            "clicks": self._clicks,
            "descents": self._descents,
            "back_ok": self._back_ok,
            "back_failed": self._back_failed,
            "relaunches": self._relaunches,
            "reclick_recoveries": self._reclicks,
            "left_app": self._left_app,
            "lost_returns": self._lost,
            "dialog_recoveries": self._dialog_recoveries,
            "foreign_screens_skipped": self._foreign_skipped,
            "clear_between_paths": self.clear_between_paths,
            "keypad_keys_skipped": self._keypad_skipped,
            "incidents": self._incidents,
            "back_trace": self._back_trace[:40],
            "elapsed_seconds": round(time.time() - self._started, 1),
            "guard": self.guard.summary(),
        }

    def _checkpoint(self) -> None:
        try:
            self.write()
        except OSError as exc:
            logger.warning("Checkpoint failed: %s", exc)

    def write(self, path: Optional[Path] = None) -> Path:
        target = path or (self.output_dir / "menutree_rows.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "format": "menutree-rows/1",
                    "app_package": self.package,
                    "rows": [r.to_dict() for r in self.rows],
                    "stats": self.stats(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return target
