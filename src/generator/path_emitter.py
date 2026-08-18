"""Turn MenuTree graph paths into UVTA testcases -- deterministically.

The LLM is deliberately not in this path. Same utg.js in => byte-identical
.uvta out, which is what makes a release gate triageable. Anything the LLM
contributes later (nicer names, suite grouping) is layered on top as an
optional, non-structural pass.

Coverage model: one testcase per *actionable transition* (edge coverage).
Each testcase is [shortest path from root to edge.from_state] + [that edge].
Node coverage falls out of edge coverage for free.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..parser.menu_tree import MenuTree, Selector, Transition
from . import uvta_syntax as uvta

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9]+")

_TOUCH_TYPES = {"touch"}
_LONG_TOUCH_TYPES = {"long_touch", "longtouch"}
_SET_TEXT_TYPES = {"set_text", "settext"}
_SCROLL_TYPES = {"scroll"}
_KEY_TYPES = {"key"}


@dataclass
class TestCase:
    name: str
    steps: List[str] = field(default_factory=list)
    target_state: str = ""
    target_activity: str = ""
    depth: int = 0

    def render(self) -> str:
        return "\n".join([uvta.testcase_header(self.name), *self.steps])


class UnsupportedEventType(Exception):
    pass


class PathEmitter:
    def __init__(self, config: dict, package: str):
        self.package = package
        self.verify_timeout = float(
            config.get("verify_timeout", uvta.DEFAULT_VERIFY_TIMEOUT)
        )
        self.verify_after_each_step = config.get("verify_after_each_step", True)
        self.max_depth = int(config.get("max_path_depth", 0)) or None
        self.skipped: List[str] = []

    # -- public ----------------------------------------------------------
    def emit(self, tree: MenuTree) -> List[TestCase]:
        paths = tree.shortest_paths_from_root()
        cases: List[TestCase] = []
        used_names: Dict[str, int] = {}

        for edge in tree.actionable_transitions():
            prefix = paths.get(edge.from_state)
            if prefix is None:
                self.skipped.append(
                    f"event {edge.event_id}: source state unreachable from root"
                )
                continue

            full_path = prefix + [edge]
            if self.max_depth and len(full_path) > self.max_depth:
                self.skipped.append(
                    f"event {edge.event_id}: path depth {len(full_path)} "
                    f"exceeds max_path_depth {self.max_depth}"
                )
                continue

            try:
                steps = self._render_path(full_path)
            except UnsupportedEventType as exc:
                self.skipped.append(f"event {edge.event_id}: {exc}")
                continue

            name = self._unique_name(self._name_for(tree, edge), used_names)
            cases.append(
                TestCase(
                    name=name,
                    steps=steps,
                    target_state=edge.to_state,
                    target_activity=tree.states[edge.to_state].activity
                    if edge.to_state in tree.states
                    else "",
                    depth=len(full_path),
                )
            )

        # Stable ordering: shallow paths first, then by name.
        cases.sort(key=lambda c: (c.depth, c.name))
        if self.skipped:
            logger.warning(
                "%d transition(s) produced no testcase; see the coverage report.",
                len(self.skipped),
            )
        logger.info("Emitted %d deterministic testcase(s).", len(cases))
        return cases

    # -- rendering -------------------------------------------------------
    def _render_path(self, path: List[Transition]) -> List[str]:
        steps = [uvta.launch(self.package)]
        for transition in path:
            steps.extend(self._render_transition(transition))
        return steps

    def _render_transition(self, t: Transition) -> List[str]:
        event_type = t.event_type

        if event_type in _KEY_TYPES:
            return [uvta.press_key(t.key_name or "BACK")]

        if t.selector is None:
            raise UnsupportedEventType(
                f"{event_type or 'unknown'} event has no resolvable selector"
            )

        if event_type in _TOUCH_TYPES:
            return self._with_verify(uvta.click(t.selector), t.selector)
        if event_type in _LONG_TOUCH_TYPES:
            return self._with_verify(uvta.long_click(t.selector), t.selector)
        if event_type in _SET_TEXT_TYPES:
            return [uvta.set_text(t.selector, t.input_text or "")]
        if event_type in _SCROLL_TYPES:
            return [uvta.scroll(t.selector, "down")]

        raise UnsupportedEventType(f"unsupported event type '{event_type}'")

    def _with_verify(self, action: str, selector: Selector) -> List[str]:
        if not self.verify_after_each_step:
            return [action]
        return [action, uvta.verify_exists(selector, self.verify_timeout)]

    # -- naming ----------------------------------------------------------
    def _name_for(self, tree: MenuTree, edge: Transition) -> str:
        activity = ""
        if edge.from_state in tree.states:
            activity = tree.states[edge.from_state].short_activity
        target = edge.selector.value if edge.selector else str(edge.event_id)
        parts = [p for p in ("Verify", activity, target) if p]
        return _SAFE_NAME.sub("_", "_".join(parts)).strip("_")[:120]

    @staticmethod
    def _unique_name(base: str, used: Dict[str, int]) -> str:
        if base not in used:
            used[base] = 1
            return base
        used[base] += 1
        return f"{base}_{used[base]}"
