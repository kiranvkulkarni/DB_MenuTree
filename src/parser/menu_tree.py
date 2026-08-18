"""Data model for the reconstructed MenuTree.

A MenuTree is DroidBot's UI Transition Graph (UTG) after we have joined the
per-event view metadata back onto the edges. Unlike a flat click stream, this
retains parent/child structure, so a testcase is a *derived graph path* rather
than something an LLM has to guess.
"""
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# Event types that move the app but are not user-facing "features".
# They are valid path steps but never the *target* of a generated testcase.
NAVIGATION_EVENT_TYPES = {"key", "intent", "spawn", "kill"}
# Event types that restart the app; these must never appear inside a path,
# because every generated testcase already begins with an explicit launch.
RESTART_EVENT_TYPES = {"intent", "spawn", "kill"}


# A className-only selector cannot uniquely identify a control: a Compose
# screen contains dozens of bare `android.view.View` nodes. Tests built on one
# would click an arbitrary match, so they are reported, not emitted.
AMBIGUOUS_STRATEGIES = {"className"}


@dataclass(frozen=True)
class Selector:
    """How a test should locate a view. `strategy` matches the UVTA keyword."""
    strategy: str  # text | desc | resourceId | className
    value: str

    @property
    def is_ambiguous(self) -> bool:
        return self.strategy in AMBIGUOUS_STRATEGIES

    def __str__(self) -> str:
        return f'{self.strategy} "{self.value}"'


@dataclass(frozen=True)
class Transition:
    from_state: str
    to_state: str
    event_id: int
    event_type: str
    event_str: str
    selector: Optional[Selector] = None
    input_text: Optional[str] = None
    key_name: Optional[str] = None

    @property
    def is_navigation(self) -> bool:
        return self.event_type in NAVIGATION_EVENT_TYPES

    @property
    def is_restart(self) -> bool:
        return self.event_type in RESTART_EVENT_TYPES

    @property
    def has_reliable_selector(self) -> bool:
        return self.selector is not None and not self.selector.is_ambiguous

    @property
    def is_actionable(self) -> bool:
        """True if this edge is a user-facing option a test can reliably target."""
        return not self.is_navigation and self.has_reliable_selector

    @property
    def is_ambiguous(self) -> bool:
        """A real control we found, but cannot address uniquely."""
        return (
            not self.is_navigation
            and self.selector is not None
            and self.selector.is_ambiguous
        )


@dataclass
class MenuState:
    state_str: str
    activity: str
    package: str
    structure_str: str = ""
    screenshot: Optional[str] = None
    label: str = ""

    @property
    def short_activity(self) -> str:
        return self.activity.split(".")[-1] if self.activity else "UNKNOWN"


@dataclass
class MenuTree:
    states: Dict[str, MenuState] = field(default_factory=dict)
    transitions: List[Transition] = field(default_factory=list)
    root: Optional[str] = None
    meta: Dict = field(default_factory=dict)

    # -- adjacency -------------------------------------------------------
    def _adjacency(self) -> Dict[str, List[Transition]]:
        if not hasattr(self, "_adj_cache") or self._adj_cache is None:
            adj: Dict[str, List[Transition]] = {}
            for t in self.transitions:
                adj.setdefault(t.from_state, []).append(t)
            # Deterministic edge order -> deterministic paths -> reproducible suites.
            for edges in adj.values():
                edges.sort(key=lambda e: (e.event_id, e.event_str))
            self._adj_cache = adj
        return self._adj_cache

    def outgoing(self, state_str: str) -> List[Transition]:
        return self._adjacency().get(state_str, [])

    def invalidate_cache(self) -> None:
        self._adj_cache = None

    # -- traversal -------------------------------------------------------
    def shortest_paths_from_root(self) -> Dict[str, List[Transition]]:
        """BFS from the root state.

        Returns state_str -> list of transitions forming the shortest path.
        Restart edges are excluded: every emitted testcase starts with an
        explicit `launch`, so replaying an in-graph restart would be wrong.
        """
        if self.root is None or self.root not in self.states:
            return {}

        paths: Dict[str, List[Transition]] = {self.root: []}
        queue = deque([self.root])
        while queue:
            current = queue.popleft()
            for edge in self.outgoing(current):
                if edge.is_restart:
                    continue
                if edge.to_state in paths or edge.to_state not in self.states:
                    continue
                paths[edge.to_state] = paths[current] + [edge]
                queue.append(edge.to_state)
        return paths

    def unreachable_states(self) -> List[str]:
        reachable = set(self.shortest_paths_from_root())
        return sorted(set(self.states) - reachable)

    def dead_end_states(self) -> List[str]:
        """States with no outgoing edge other than back/restart navigation."""
        out: List[str] = []
        for state_str in self.states:
            edges = self.outgoing(state_str)
            if not any(not e.is_navigation for e in edges):
                out.append(state_str)
        return sorted(out)

    def actionable_transitions(self) -> List[Transition]:
        return sorted(
            (t for t in self.transitions if t.is_actionable),
            key=lambda t: (t.event_id, t.event_str),
        )

    def ambiguous_transitions(self) -> List[Transition]:
        """Controls found but not uniquely addressable (e.g. bare Compose views)."""
        return sorted(
            (t for t in self.transitions if t.is_ambiguous),
            key=lambda t: (t.event_id, t.event_str),
        )

    def unidentified_transitions(self) -> List[Transition]:
        """Controls with no selector at all."""
        return sorted(
            (
                t
                for t in self.transitions
                if not t.is_navigation and t.selector is None
            ),
            key=lambda t: (t.event_id, t.event_str),
        )

    def reached_activities(self) -> List[str]:
        """Activities the crawler physically reached (crawl breadth)."""
        return sorted({s.activity for s in self.states.values() if s.activity})

    def testable_activities(self) -> List[str]:
        """Activities reachable from root by launch + UI steps alone.

        This is the honest coverage number: an activity the crawler stumbled
        into but that no replayable path reaches cannot be gated on, because
        no testcase can be emitted for it.
        """
        reachable = self.shortest_paths_from_root()
        return sorted(
            {
                self.states[s].activity
                for s in reachable
                if s in self.states and self.states[s].activity
            }
        )

    def summary(self) -> Dict[str, int]:
        return {
            "states": len(self.states),
            "transitions": len(self.transitions),
            "actionable_transitions": len(self.actionable_transitions()),
            "ambiguous_transitions": len(self.ambiguous_transitions()),
            "unidentified_transitions": len(self.unidentified_transitions()),
            "reached_activities": len(self.reached_activities()),
            "testable_activities": len(self.testable_activities()),
            "unreachable_states": len(self.unreachable_states()),
            "dead_end_states": len(self.dead_end_states()),
        }
