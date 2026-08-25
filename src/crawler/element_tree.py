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

# Trailing state baked into a label. Vendor camera UIs name a control by its
# current setting -- "filteroff" becomes "filteron", "face beautyoff" becomes
# "face beautyon", "FlashOff" becomes "FlashAuto" -- so a path recorded on
# the way in cannot be replayed once anything has been toggled. Matching on
# the stem keeps replay working across state changes.
_STATE_SUFFIX = re.compile(r"(off|on|auto)$", re.IGNORECASE)


def _label_stem(label: str) -> str:
    return _STATE_SUFFIX.sub("", (label or "").strip().lower()).strip()

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
class WorkItem:
    """One unit of work: "click this element on this screen".

    The worklist is what makes coverage measurable. Recursion knows where it
    is but never what it has left to do, so it can report rows discovered and
    nothing else -- there is no denominator, and "is it 100%?" has no answer.
    An explicit item per (screen, element) gives:

        done / (done + pending + unreachable) = a real coverage figure

    and makes a run resumable, since the queue is serialisable state rather
    than a Python call stack.
    """
    screen_key: str
    label: str
    kind: str
    path: List[str]
    depth: int
    status: str = "pending"   # pending | done | unreachable | blocked | recorded
    reason: str = ""

    @property
    def key(self) -> tuple:
        return (self.screen_key, self.label, self.kind)

    def to_dict(self) -> dict:
        return {
            "screen": self.screen_key[:12],
            "label": self.label,
            "kind": self.kind,
            "depth": self.depth,
            "path": self.path,
            "status": self.status,
            "reason": self.reason,
        }


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
        # Start every run from a fresh install state. Makes runs
        # comparable, and brings back first-run pop-ups that a previous
        # run would otherwise have permanently dismissed.
        self.reset_before_start = bool(config.get("reset_before_start", True))

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
        self._processed = 0
        self._nav_forward = 0
        self._nav_back = 0
        self._nav_trace: List[Dict] = []
        self._stem_matches = 0
        self._worklist: Dict[tuple, WorkItem] = {}
        self._screen_elements: Dict[str, List[Element]] = {}
        self._screen_paths: Dict[str, List[str]] = {}
        self._row_for: Dict[tuple, TreeNode] = {}

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
            # Clean launch, not a task resume: another app's activity may be
            # stacked on top of ours (the Gallery ends up inside the camera's
            # task after "Latest Photos"), and resuming would land there.
            if not self.driver.launch_clean(self.package, clear=clear):
                self.driver.start_app(self.package, clear=clear)
        except DriverError as exc:
            logger.warning("Relaunch failed: %s", exc)
            return False

        key, views, _ = self._await_stable()
        for label in path:
            element = self._find_element(label, views)
            if element is None:
                return False
            if not self._click(element, views):
                return False
            key, views, _ = self._await_stable()

        # Verify by element overlap, never by exact state key.
        #
        # The key encodes affordance text, and the camera puts its flash mode
        # in the label -- FlashOff / FlashAuto / FlashOn. So relaunching onto
        # the correct screen with a different flash state yields a different
        # key, and an exact test calls that a failed replay. Measured: 38 of
        # 41 navigations failed this way, including ones whose path was empty
        # (relaunch, click nothing, be at the root) which cannot fail for any
        # navigational reason.
        #
        # _is_same_screen was corrected for exactly this in fcc9967; this
        # second comparison was missed and kept the bug alive.
        if key == target_key:
            return True
        target_elements = self._screen_elements.get(target_key)
        if target_elements and views:
            return self._is_same_screen(views, target_elements)
        return False

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

    # -- worklist --------------------------------------------------------
    def _register_screen(
        self, screen_key: str, views: Sequence[Dict], path: List[str], depth: int
    ) -> int:
        """Record every element on a screen; queue the actionable ones.

        This is the "list all UI elements of that screen" step. Every element
        becomes a row immediately (that is the breadth, and the sheet wants
        titles and static text too). Only the ones worth pressing become
        pending work.
        """
        if screen_key in self._visited_screens:
            return 0
        self._visited_screens.add(screen_key)

        elements = self._elements(views)
        self._screen_elements[screen_key] = list(elements)
        self._screen_paths[screen_key] = list(path)

        logger.info(
            "screen %-12s depth %-2d  %-38s  %d element(s)",
            screen_key[:12], depth, " > ".join(path[-2:]) or "<root>",
            len(elements),
        )

        queued = 0
        for element in elements:
            blocked = (
                self.guard.blocks("text", element.label)
                if element.interactive else None
            )
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
            self._row_for[(screen_key, element.label, element.kind)] = row
            if len(self.rows) % self.checkpoint_every == 0:
                self._checkpoint()

            item = WorkItem(
                screen_key=screen_key,
                label=element.label,
                kind=element.kind,
                path=list(path),
                depth=depth,
            )

            if KEYPAD_KEY.fullmatch(element.label.strip()):
                item.status, item.reason = "recorded", "keypad key -- not pressed"
                row.note = "keypad key -- recorded, not pressed"
                self._keypad_skipped += 1
            elif blocked:
                item.status, item.reason = "blocked", f"action guard: {blocked}"
            elif not element.interactive or element.kind == "back":
                item.status = "recorded"
                item.reason = "not interactive" if not element.interactive else "back"
            elif depth >= self.max_depth:
                item.status, item.reason = "recorded", f"at max_depth {self.max_depth}"
            else:
                queued += 1

            self._worklist[item.key] = item

        return queued

    def _pending_items(self) -> List[WorkItem]:
        return [i for i in self._worklist.values() if i.status == "pending"]

    def _next_item(self, current_screen: Optional[str]) -> Optional[WorkItem]:
        """Cheapest pending item.

        Prefer one on the screen already in front of us -- navigation is the
        dominant cost (a relaunch is ~30s), so exhausting the current screen
        before moving is worth far more than any traversal-order purity.
        Otherwise take the shallowest, which keeps replay paths short.
        """
        pending = self._pending_items()
        if not pending:
            return None
        if current_screen:
            here = [i for i in pending if i.screen_key == current_screen]
            if here:
                return here[0]
        return min(pending, key=lambda i: i.depth)

    def _identify_current(self, views: Sequence[Dict]) -> Optional[str]:
        """Which known screen are we on? Matched by element overlap."""
        if not views:
            return None
        here = self._elements(views)
        best, best_score = None, 0.0
        for key, elements in self._screen_elements.items():
            score = screen_similarity(elements, here)
            if score > best_score:
                best, best_score = key, score
        return best if best_score >= self.return_similarity else None

    def _find_element(
        self, label: str, views: Sequence[Dict], rid: Optional[str] = None
    ) -> Optional[Element]:
        """Locate an element by label, tolerating a changed toggle state."""
        elements = [e for e in self._elements(views) if e.interactive]
        exact = next((e for e in elements if e.label == label), None)
        if exact is not None:
            return exact
        if rid:
            by_rid = next((e for e in elements if e.resource_id == rid), None)
            if by_rid is not None:
                return by_rid
        stem = _label_stem(label)
        if stem:
            near = [e for e in elements if _label_stem(e.label) == stem]
            if len(near) == 1:
                self._stem_matches += 1
                return near[0]
        return None

    def _click_label(self, label: str, views: Sequence[Dict]) -> bool:
        live = self._find_element(label, views)
        return bool(live and self._click(live, views))

    def _navigate_to(self, item: WorkItem) -> Tuple[bool, List[Dict]]:
        """Get to the screen this item lives on.

        Direction matters, and BACK only goes one way. The recursive walker
        only ever returned to the screen it had just left -- always upward --
        so BACK-first was correct there. A worklist jumps to arbitrary
        screens, and using BACK to reach one *deeper* than the current
        position walks away from it: asked for the SubSet settings menu, BACK
        delivered the camera main screen instead, every time. That was 82 of
        91 navigations failing into a ~20s relaunch each.

        Both paths are known, so the relationship decides the move:
          target extends here  -> click forward through the extra labels
          target is an ancestor -> BACK the difference
          unrelated            -> relaunch and replay from launch
        """
        target = self._screen_elements.get(item.screen_key)
        key, views, _ = self._await_stable()

        if views and target and self._is_same_screen(views, target):
            return True, views
        if key == item.screen_key and views:
            return True, views

        here_key = self._identify_current(views)
        here_path = self._screen_paths.get(here_key) if here_key else None

        # Every navigation decision is recorded. Three hypotheses about this
        # code were wrong, twice because a fix shipped before the premise was
        # checked. The trace says which branch was taken and whether it
        # landed, so the next change is driven by evidence.
        trace = {
            "want": item.screen_key[:10],
            "want_path": list(item.path),
            "here": here_key[:10] if here_key else None,
            "here_path": here_path,
            "identified": here_key is not None,
            "branch": None,
            "ok": False,
        }
        self._nav_trace.append(trace)

        if here_path is not None:
            target_path = item.path

            # Target sits below us: click down into it.
            if (len(target_path) > len(here_path)
                    and target_path[:len(here_path)] == here_path):
                ok = True
                for label in target_path[len(here_path):]:
                    _, views, _ = self._await_stable()
                    if not self._click_label(label, views):
                        ok = False
                        break
                trace["branch"] = "forward"
                if ok:
                    _, views, _ = self._await_stable()
                    if views and target and self._is_same_screen(views, target):
                        self._nav_forward += 1
                        trace["ok"] = True
                        return True, views

            # Target sits above us: BACK out the difference.
            elif (len(target_path) < len(here_path)
                    and here_path[:len(target_path)] == target_path):
                for _ in range(len(here_path) - len(target_path)):
                    try:
                        self.driver.press_back()  # type: ignore[union-attr]
                    except DriverError:
                        break
                trace["branch"] = "back"
                _, views, _ = self._await_stable()
                if views and target and self._is_same_screen(views, target):
                    self._nav_back += 1
                    trace["ok"] = True
                    return True, views

        # Unrelated screen, or the shortcut did not land: replay from launch.
        if trace["branch"] is None:
            trace["branch"] = "replay"
        if self._relaunch_and_replay(item.screen_key, item.path):
            _, views, _ = self._await_stable()
            trace["ok"] = True
            trace["branch"] += "+replay_ok"
            return bool(views), views
        trace["branch"] += "+replay_failed"
        return False, []

    def _process(self, item: WorkItem) -> None:
        """Navigate to the item's screen, click it, record what happened."""
        row = self._row_for.get((item.screen_key, item.label, item.kind))
        reached, views = self._navigate_to(item)
        if not reached or not views:
            item.status = "unreachable"
            item.reason = "could not navigate to the parent screen"
            self._lost += 1
            if row is not None:
                row.note = "NOT TESTED -- could not return to parent screen"
            logger.warning("unreachable: %r on %s",
                           item.label, item.screen_key[:12])
            return

        live = self._find_element(item.label, views)
        if live is None and self.clear_between_paths:
            # The element was here a moment ago and is gone. Usually a
            # one-shot dialog: a sibling ("Cancel") already dismissed it, so
            # the other branch ("Turn on") no longer exists. Clearing and
            # replaying restores first-run state, making it reappear.
            if self._relaunch_and_replay(item.screen_key, item.path, clear=True):
                _, views, _ = self._await_stable()
                live = self._find_element(item.label, views)
                if live is not None:
                    self._dialog_recoveries += 1
                    if row is not None:
                        row.note = "recovered via clear+relaunch (one-shot dialog)"

        if live is None or not self._click(live, views):
            item.status = "unreachable"
            item.reason = "element vanished before it could be clicked"
            if row is not None and not row.note:
                row.note = "element vanished before click (one-shot?)"
            return

        after_key, after_views, after_pkg = self._await_stable()
        after_probe = self._elements(after_views) if after_views else []

        dialog = self._detect_system_dialog(after_probe)
        if dialog:
            self._dismiss_system_dialog(after_probe, after_views, dialog)
            item.status, item.reason = "done", f"{dialog} dialog handled"
            return

        if not after_key or after_key == EMPTY_STATE:
            item.status, item.reason = "done", "no readable screen after click"
            return

        source = self._screen_elements.get(item.screen_key, [])
        similarity = screen_similarity(source, after_probe)

        if similarity < self.similarity_threshold:
            # A different screen: this element opens a submenu.
            item.status, item.reason = "done", "opened a screen"
            if row is not None:
                row.descended = True
            self._descents += 1

            foreign = bool(after_pkg and after_pkg != self.package)
            if foreign and row is not None:
                row.note = f"leaves app -> {after_pkg}"

            if foreign and not looks_like_dialog(after_views, self.package):
                # A real pop-up over the app is in scope (a permission prompt
                # is owned entirely by the permission controller). An
                # unrelated app is not -- enumerating a browser's menu as if
                # it were this app's would be worse than missing it.
                self._foreign_skipped += 1
                logger.info("foreign screen %s -- recorded, not walked", after_pkg)
            else:
                self._register_screen(
                    after_key, after_views, item.path + [item.label], item.depth + 1
                )
        else:
            item.status, item.reason = "done", "selection on the same screen"
            if row is not None:
                row.note = row.note or "selection"

    # -- public ----------------------------------------------------------
    def walk(self) -> List[TreeNode]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.driver = make_driver(self.serial, self.backend, self.settle)
        ime = self.driver.current_ime_package()
        if ime and ime not in self._exclude:
            self._exclude.append(ime)
            logger.info("Excluding active keyboard package: %s", ime)
        self._started = time.time()

        # Start from a fresh copy of the app. Beyond reproducibility, this is
        # what brings first-run pop-ups back -- permission prompts, one-shot
        # dialogs, onboarding -- so they are part of the tree rather than
        # something a previous run permanently dismissed.
        if self.reset_before_start:
            logger.info("Resetting %s to a fresh state before exploring",
                        self.package)
        if not self.driver.launch_clean(self.package, clear=self.reset_before_start):
            self.driver.start_app(self.package, clear=self.reset_before_start)

        self.rows.append(TreeNode(label=self.package, raw_label=self.package,
                                  kind="root", depth=1, path=[]))

        root_key, views, current = self._await_stable()
        if not root_key or root_key == EMPTY_STATE or not views:
            logger.error("Could not read a launch screen for %s", self.package)
            return self.rows

        # Refuse to root the tree in another app. If something else is in
        # front at launch -- a leftover Gallery, a share sheet, whatever the
        # previous session left behind -- registering it as the root poisons
        # every navigation after it: the walker spends the whole run trying
        # to return to a screen belonging to a different app. Observed: a run
        # whose root was recorded as the Gallery (Open Photos / Share /
        # Favourite) and which reached 13 rows before giving up.
        if current and current != self.package:
            logger.warning(
                "Launch landed in %s, not %s -- retrying once", current, self.package,
            )
            self.driver.start_app(self.package, clear=False)
            root_key, views, current = self._await_stable()
            if current and current != self.package:
                logger.error(
                    "Cannot start %s: %s is in the foreground. Refusing to "
                    "root the tree in another app.", self.package, current,
                )
                return self.rows

        self._register_screen(root_key, views, [], 2)

        # The worklist loop. Every iteration takes one pending element,
        # reaches it, presses it, and marks it done -- so at any moment the
        # remaining work is known, which is what makes the coverage figure
        # real rather than asserted.
        while self._budget_left():
            _, _, current_pkg = (None, None, None)
            here_key, _, _ = self._await_stable()
            item = self._next_item(here_key)
            if item is None:
                logger.info("Worklist exhausted -- every element traversed.")
                break
            self._process(item)
            self._processed += 1
            if self._processed % self.checkpoint_every == 0:
                self._checkpoint()

        remaining = len(self._pending_items())
        if remaining:
            logger.warning(
                "Stopped with %d element(s) still pending (budget reached). "
                "Coverage is partial and the report says so.", remaining,
            )

        logger.info("Walk finished: %s", self.stats())
        return self.rows

    def stats(self) -> Dict:
        by_depth: Dict[int, int] = {}
        for row in self.rows:
            by_depth[row.depth] = by_depth.get(row.depth, 0) + 1
        status = {}
        for item in self._worklist.values():
            status[item.status] = status.get(item.status, 0) + 1
        actionable = sum(
            v for k, v in status.items()
            if k in ("done", "pending", "unreachable")
        )
        traversed = status.get("done", 0)
        coverage = round(100.0 * traversed / actionable, 1) if actionable else 0.0

        return {
            "rows": len(self.rows),
            "max_depth": max((r.depth for r in self.rows), default=0),
            "rows_by_depth": dict(sorted(by_depth.items())),
            # The coverage figure, and the numbers behind it. `actionable`
            # excludes elements never meant to be pressed (static text, back
            # buttons, keypad keys, guard-blocked) so the percentage is not
            # inflated by rows that were never work in the first place.
            "coverage_percent": coverage,
            "worklist_total": len(self._worklist),
            "worklist_actionable": actionable,
            "worklist_by_status": dict(sorted(status.items())),
            "elements_pending": status.get("pending", 0),
            "elements_unreachable": status.get("unreachable", 0),
            "screens_visited": len(self._visited_screens),
            "clicks": self._clicks,
            "descents": self._descents,
            "back_ok": self._back_ok,
            "back_failed": self._back_failed,
            "relaunches": self._relaunches,
            "reclick_recoveries": self._reclicks,
            "nav_forward": self._nav_forward,
            "nav_back": self._nav_back,
            "nav_trace": self._nav_trace[:60],
            "stem_matches": self._stem_matches,
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
