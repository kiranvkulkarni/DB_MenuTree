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
from .hierarchy import EMPTY_STATE, center_of, parse_hierarchy, state_key

logger = logging.getLogger(__name__)


@dataclass
class TreeNode:
    """One row of the MenuTree."""
    label: str
    kind: str
    depth: int
    path: List[str] = field(default_factory=list)
    interactive: bool = False
    descended: bool = False
    blocked: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
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
        self.back_attempts = int(config.get("back_attempts", 2))
        self.checkpoint_every = int(config.get("checkpoint_every", 25))

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
    def _return_to(self, target_key: str, path: List[str]) -> bool:
        """Get back to the screen we descended from.

        BACK first, because it is cheap and keeps the session. Relaunch and
        replay only when BACK fails to land us where we started.
        """
        assert self.driver is not None
        for _ in range(self.back_attempts):
            try:
                self.driver.press_back()
            except DriverError:
                break
            key, _, current = self._await_stable()
            if key == target_key:
                self._back_ok += 1
                return True
            if current and current != self.package:
                # BACK walked us out of the app entirely.
                self._left_app += 1
                break

        self._back_failed += 1
        return self._relaunch_and_replay(target_key, path)

    def _relaunch_and_replay(self, target_key: str, path: List[str]) -> bool:
        """Fallback: restart the app and re-walk the labels that got us here."""
        assert self.driver is not None
        self._relaunches += 1
        try:
            self.driver.start_app(self.package, clear=False)
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

        screen_key, views, _ = self._await_stable()
        if not screen_key or screen_key == EMPTY_STATE:
            return
        if screen_key in self._visited_screens:
            return
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
                kind=element.kind,
                depth=depth,
                path=list(path),
                interactive=element.interactive,
                blocked=blocked,
            )
            self.rows.append(row)
            if len(self.rows) % self.checkpoint_every == 0:
                self._checkpoint()

            if not element.interactive or blocked or element.kind == "back":
                continue

            # Re-read the screen: an earlier click may have shifted indices.
            current_key, current_views, _ = self._await_stable()
            if current_key != screen_key:
                if not self._return_to(screen_key, path):
                    return
                current_key, current_views, _ = self._await_stable()
            live = next(
                (e for e in self._elements(current_views)
                 if e.label == element.label and e.interactive),
                None,
            )
            if live is None or not self._click(live, current_views):
                continue

            after_key, after_views, after_pkg = self._await_stable()
            if not after_key or after_key == EMPTY_STATE:
                self._return_to(screen_key, path)
                continue

            after_elements = self._elements(after_views)
            similarity = screen_similarity(elements, after_elements)

            if after_key != screen_key and similarity < self.similarity_threshold:
                row.descended = True
                self._descents += 1
                if after_pkg and after_pkg != self.package:
                    row.note = f"leaves app -> {after_pkg}"
                self._walk(depth + 1, path + [element.label])
                self._return_to(screen_key, path)
            elif after_key != screen_key:
                # Same items, different key: a selection or toggle, not a menu.
                row.note = "selection"
                self._return_to(screen_key, path)

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
        self.rows.append(TreeNode(label=self.package, kind="root", depth=1, path=[]))
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
            "left_app": self._left_app,
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
