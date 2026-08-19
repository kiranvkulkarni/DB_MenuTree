"""Deterministic replay-based explorer.

Instead of walking forward and pressing BACK to backtrack, this explores by
replaying a known path from a clean launch every time:

    frontier = [(path_to_state, unexplored_action), ...]
    for each item:  launch -> replay path -> perform action -> capture state

Costs O(depth) actions per state discovered rather than O(1), and buys three
things a backtracking crawler cannot give:

  * Reproducibility. No dependence on BACK landing where you assume, and no
    accumulated state drift, so the same build yields the same graph. A gate
    built on baseline diffing needs this: otherwise a coverage drop is
    ambiguous between an app regression and the crawler wandering elsewhere.
  * Every path is verified executable, because it was just executed. The
    crawl *is* the replay pass, so emitted tests are known-good by
    construction.
  * Full control of the state abstraction and selector resolution at capture
    time rather than reconstructed afterwards.

Exploration order is a deterministic BFS: the frontier is FIFO and actions
within a state are taken in document order.
"""
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..parser.menu_tree import Selector
from ..parser.selectors import SelectorResolver
from .device_driver import DeviceDriver, DriverError, make_driver
from .hierarchy import (
    EMPTY_STATE,
    center_of,
    interactive_views,
    parse_hierarchy,
    state_key,
)

logger = logging.getLogger(__name__)


@dataclass
class Step:
    """One replayable action."""
    action: str  # click | long_click
    selector_strategy: str
    selector_value: str
    x: int
    y: int

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "selector_strategy": self.selector_strategy,
            "selector_value": self.selector_value,
            "x": self.x,
            "y": self.y,
        }


@dataclass
class ExploredState:
    key: str
    activity: str
    package: str
    path: List[Step]
    screenshot: Optional[str] = None
    view_count: int = 0


@dataclass
class ExploredEdge:
    from_state: str
    to_state: str
    step: Step
    order: int


@dataclass
class ExplorationResult:
    package: str
    root: Optional[str] = None
    states: Dict[str, ExploredState] = field(default_factory=dict)
    edges: List[ExploredEdge] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "format": "menutree/1",
            "app_package": self.package,
            "root": self.root,
            "states": [
                {
                    "key": s.key,
                    "activity": s.activity,
                    "package": s.package,
                    "depth": len(s.path),
                    "screenshot": s.screenshot,
                    "view_count": s.view_count,
                    "path": [step.to_dict() for step in s.path],
                }
                for s in self.states.values()
            ],
            "edges": [
                {
                    "from": e.from_state,
                    "to": e.to_state,
                    "order": e.order,
                    **e.step.to_dict(),
                }
                for e in self.edges
            ],
            "stats": self.stats,
        }


class ReplayExplorer:
    def __init__(self, package: str, serial: Optional[str], config: dict):
        self.package = package
        self.serial = serial
        self.config = config

        self.output_dir = Path(config.get("output_dir", "./u2_out"))
        self.max_states = int(config.get("max_states", 300))
        self.max_actions = int(config.get("max_actions", 3000))
        self.max_depth = int(config.get("max_depth", 8))
        self.time_budget = float(config.get("time_budget", 900))
        self.settle = float(config.get("settle_seconds", 1.0))
        self.state_mode = config.get("state_key_mode", "affordance")
        # Defaults ON: without it any one-time UI the app records as
        # dismissed makes the recorded root unreachable and the crawl
        # silently collapses. Measured on the Phone app: 71/74 replays
        # drifted without it, 3/38 with it (9 states -> 22).
        self.clear_between_paths = config.get("clear_between_paths", True)
        self.capture_screenshots = config.get("capture_screenshots", True)
        self.backend = config.get("driver", "auto")
        self.ready_timeout = float(config.get("ready_timeout", 12.0))
        self.stable_interval = float(config.get("stable_interval", 0.4))
        self.checkpoint_every = int(config.get("checkpoint_every", 5))

        self.resolver = SelectorResolver(
            priority=config.get(
                "selector_priority",
                ("text", "content_description", "resource_id"),
            ),
            fallback_to_class=config.get("fallback_to_class", True),
            resolve_descendants=config.get("resolve_descendant_labels", True),
            max_descendant_depth=int(config.get("max_descendant_depth", 4)),
        )

        self.driver: Optional[DeviceDriver] = None
        self.result = ExplorationResult(package=package)
        self._actions_taken = 0
        self._replays = 0
        self._started = 0.0
        self._left_app = 0
        self._empty_dumps = 0
        self._unsettled = 0
        self._drifted = 0
        self._forward_steps = 0

    # -- capture ---------------------------------------------------------
    def _capture(self) -> Tuple[Optional[str], List[Dict], str]:
        """Return (state_key, views, activity_package) for the current screen."""
        assert self.driver is not None
        xml = self.driver.dump_hierarchy()
        views = parse_hierarchy(xml)
        if not views:
            return None, [], ""
        current = self.driver.current_package() or ""
        key = state_key(views, self.state_mode, self.package)
        return key, views, current

    def _await_stable(self, require_app: bool = True) -> Tuple[Optional[str], List[Dict], str]:
        """Capture once the screen has stopped changing.

        Two things make a naive capture wrong, and both produce a *different
        state key on every run*, which destroys reproducibility:

          * A dump during the launch animation holds only launcher views and
            hashes to EMPTY_STATE.
          * Compose lays out asynchronously, so an early dump catches a
            partially built tree -- observed here as 57 views against the 80
            the settled screen has.

        So poll until two consecutive dumps agree on the key (quiescence)
        rather than until the screen merely looks non-empty. Applied after
        every action, not just at launch, since transitions animate too.
        """
        deadline = time.time() + self.ready_timeout
        previous_key, views, current = self._capture()
        settled_since = None

        while time.time() < deadline:
            usable = bool(previous_key) and previous_key != EMPTY_STATE
            if usable and require_app and current != self.package:
                usable = False

            if usable:
                if settled_since == previous_key:
                    return previous_key, views, current
                settled_since = previous_key

            time.sleep(self.stable_interval)
            previous_key, views, current = self._capture()

        self._unsettled += 1
        return previous_key, views, current

    def _await_app(self) -> Tuple[Optional[str], List[Dict], str]:
        return self._await_stable(require_app=True)

    def _record_state(
        self, key: str, views: List[Dict], path: List[Step], activity_pkg: str
    ) -> ExploredState:
        activity = None
        if self.driver is not None:
            activity = self.driver.current_activity()
        state = ExploredState(
            key=key,
            activity=activity or activity_pkg or self.package,
            package=self.package,
            path=list(path),
            view_count=len(views),
        )
        if self.capture_screenshots:
            shots = self.output_dir / "states"
            shots.mkdir(parents=True, exist_ok=True)
            target = shots / f"{key}.png"
            if not target.exists() and self.driver is not None:
                if self.driver.screenshot(str(target)):
                    state.screenshot = f"states/{key}.png"
            else:
                state.screenshot = f"states/{key}.png"
        self.result.states[key] = state
        return state

    # -- replay ----------------------------------------------------------
    def _replay(self, path: List[Step]) -> Optional[str]:
        """Launch clean and replay `path`. Returns the resulting state key."""
        assert self.driver is not None
        self.driver.start_app(self.package, clear=self.clear_between_paths)
        self._replays += 1

        key, views, _ = self._await_app()
        if not key or key == EMPTY_STATE:
            return None

        for step in path:
            if not self._perform(step, views):
                return None
            self._actions_taken += 1
            key, views, current = self._await_stable(require_app=False)
            if not key or key == EMPTY_STATE:
                return None
            if current and current != self.package:
                return None
        return key

    def _perform(self, step: Step, views: List[Dict]) -> bool:
        """Re-locate the step's target in the current screen, then act.

        Prefers matching by selector so replay survives minor layout shifts;
        falls back to the recorded coordinates.
        """
        assert self.driver is not None
        x, y = step.x, step.y

        match = self._find_by_selector(
            views, step.selector_strategy, step.selector_value
        )
        if match is not None:
            centre = center_of(views[match])
            if centre:
                x, y = centre

        try:
            if step.action == "long_click":
                self.driver.long_tap(x, y)
            else:
                self.driver.tap(x, y)
        except DriverError as exc:
            logger.debug("action failed: %s", exc)
            return False
        return True

    @staticmethod
    def _find_by_selector(
        views: List[Dict], strategy: str, value: str
    ) -> Optional[int]:
        key = {
            "text": "text",
            "desc": "content_description",
            "resourceId": "resource_id",
        }.get(strategy)
        if key is None:
            return None
        for index, view in enumerate(views):
            candidate = view.get(key)
            if not candidate:
                continue
            candidate = str(candidate)
            if key == "resource_id" and "/" in candidate:
                candidate = candidate.split("/")[-1]
            if candidate.strip() == value:
                return index
        return None

    # -- explore ---------------------------------------------------------
    def _enumerate_actions(self, views: List[Dict]) -> List[Step]:
        steps: List[Step] = []
        seen = set()
        for index in interactive_views(views, self.package):
            view = views[index]
            centre = center_of(view)
            if centre is None:
                continue
            selector = self.resolver.resolve(view, views)
            if selector is None:
                continue
            action = "long_click" if (
                view.get("long_clickable") and not view.get("clickable")
            ) else "click"
            dedupe = (action, selector.strategy, selector.value)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            steps.append(
                Step(action, selector.strategy, selector.value, centre[0], centre[1])
            )
        return steps

    def _budget_exhausted(self) -> Optional[str]:
        if len(self.result.states) >= self.max_states:
            return f"max_states ({self.max_states}) reached"
        if self._actions_taken >= self.max_actions:
            return f"max_actions ({self.max_actions}) reached"
        elapsed = time.time() - self._started
        if elapsed >= self.time_budget:
            return f"time_budget ({self.time_budget:.0f}s) reached"
        return None

    def explore(self) -> ExplorationResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.driver = make_driver(self.serial, self.backend, self.settle)
        self._started = time.time()

        # Root: the app's state immediately after a clean launch.
        root_key = self._replay([])
        if root_key is None:
            raise DriverError(
                f"Could not capture a launch state for {self.package}. "
                "The app may not have come to the foreground."
            )
        _, views, current = self._await_app()
        self._record_state(root_key, views, [], current)
        self.result.root = root_key
        logger.info("Root state %s (%d views)", root_key[:12], len(views))

        # FIFO frontier -> deterministic breadth-first exploration.
        frontier = deque(
            (root_key, [], step) for step in self._enumerate_actions(views)
        )
        queued = {(root_key, s.selector_strategy, s.selector_value) for s in
                  self._enumerate_actions(views)}

        explored: set = set()
        stop_reason = "frontier exhausted"
        while frontier:
            reason = self._budget_exhausted()
            if reason:
                stop_reason = reason
                logger.info("Stopping: %s", reason)
                break

            from_key, path, step = frontier.popleft()
            if len(path) + 1 > self.max_depth:
                continue
            if (from_key, step.selector_strategy, step.selector_value) in explored:
                continue

            reached = self._replay(path)
            if reached != from_key:
                self._drifted += 1
                logger.debug(
                    "replay drift: expected %s got %s", from_key[:8],
                    (reached or "none")[:8],
                )
                continue

            # Having paid for the replay, keep walking forward from wherever
            # each action lands instead of restarting for the next one. The
            # prefix is what costs time, so amortise it. Order stays fixed
            # (document order, first unexplored action), so this remains
            # deterministic -- it only changes how often we pay to restart.
            current_key, current_path, current_step = from_key, path, step
            while True:
                if self._budget_exhausted():
                    break

                _, views, _ = self._await_stable(require_app=False)
                explored.add(
                    (current_key, current_step.selector_strategy,
                     current_step.selector_value)
                )
                if not self._perform(current_step, views):
                    break
                self._actions_taken += 1

                new_key, new_views, current = self._await_stable(require_app=False)
                if not new_key or new_key == EMPTY_STATE:
                    self._empty_dumps += 1
                    break
                if current and current != self.package:
                    self._left_app += 1
                    break

                new_path = current_path + [current_step]
                self.result.edges.append(
                    ExploredEdge(current_key, new_key, current_step,
                                 len(self.result.edges) + 1)
                )

                if new_key not in self.result.states:
                    self._record_state(new_key, new_views, new_path, current)
                    logger.info(
                        "State %2d: %s depth=%d via %s \"%s\"",
                        len(self.result.states), new_key[:12], len(new_path),
                        current_step.selector_strategy, current_step.selector_value,
                    )
                    if (
                        self.checkpoint_every
                        and len(self.result.states) % self.checkpoint_every == 0
                    ):
                        self._checkpoint()

                next_actions = self._enumerate_actions(new_views)
                for next_step in next_actions:
                    token = (new_key, next_step.selector_strategy,
                             next_step.selector_value)
                    if token in queued or token in explored:
                        continue
                    queued.add(token)
                    frontier.append((new_key, new_path, next_step))

                if len(new_path) >= self.max_depth:
                    break
                onward = next(
                    (
                        s for s in next_actions
                        if (new_key, s.selector_strategy, s.selector_value)
                        not in explored
                    ),
                    None,
                )
                if onward is None:
                    break
                self._forward_steps += 1
                current_key, current_path, current_step = new_key, new_path, onward

        self._finalise_stats(stop_reason, len(frontier))
        self._warn_on_drift()
        logger.info("Exploration finished: %s", self.result.stats)
        return self.result

    def _warn_on_drift(self) -> None:
        """Drift silently discards branches, so say so loudly.

        A replay that does not land where the path was recorded is thrown
        away, and that branch is never revisited. A high rate means the graph
        is badly incomplete, but nothing else in the output makes that
        obvious -- it just looks like a small app.

        The usual cause is the app changing irreversibly during the crawl: a
        one-time banner dismissed, onboarding completed, a "don't show again"
        checked. The recorded root then becomes unreachable and *everything*
        drifts. `clear_between_paths` restores the app to a fixed starting
        point and fixes it.
        """
        if not self._replays:
            return
        rate = self._drifted / self._replays
        if rate < 0.3:
            return
        logger.warning(
            "%.0f%% of replays drifted (%d/%d): they did not land where the "
            "path was recorded, so those branches were discarded and the "
            "graph is incomplete.",
            rate * 100, self._drifted, self._replays,
        )
        if not self.clear_between_paths:
            logger.warning(
                "Re-run with clear_between_paths enabled "
                "(--clear-between-paths). Without it, any one-time UI the app "
                "records as dismissed makes the recorded root unreachable."
            )

    def _checkpoint(self) -> None:
        """Persist the graph mid-crawl.

        A crawl is long and the device can die under it -- this emulator did,
        twice. Writing only at the end means one crash discards the entire
        run, so snapshot as we go.
        """
        try:
            self._finalise_stats("checkpoint", 0)
            self.write()
        except OSError as exc:
            logger.warning("Checkpoint failed: %s", exc)

    def _finalise_stats(self, stop_reason: str, frontier_remaining: int) -> None:
        self.result.stats = {
            "states": len(self.result.states),
            "edges": len(self.result.edges),
            "actions_taken": self._actions_taken,
            "replays": self._replays,
            "left_app": self._left_app,
            "empty_dumps": self._empty_dumps,
            "unsettled_captures": self._unsettled,
            "replay_drift": self._drifted,
            "forward_steps": self._forward_steps,
            "frontier_remaining": frontier_remaining,
            "elapsed_seconds": round(time.time() - self._started, 1),
            "stop_reason": stop_reason,
            "state_key_mode": self.state_mode,
            "driver": getattr(self.driver, "name", "?"),
            "descendant_selectors": self.resolver.descendant_hits,
        }

    def write(self, path: Optional[Path] = None) -> Path:
        target = path or (self.output_dir / "menutree.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.result.to_dict(), indent=2, sort_keys=False),
            encoding="utf-8",
        )
        logger.info("Graph written: %s", target.resolve())
        return target
