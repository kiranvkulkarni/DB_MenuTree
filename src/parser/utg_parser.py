"""Parse DroidBot's utg.js into a MenuTree, enriched with view selectors.

Why this exists
---------------
DroidBot already computes the UI Transition Graph -- the exact structure a
"MenuTree" needs. It writes it to <output_dir>/utg.js. Its edges, however,
only carry `event_str` and `event_id`; the *view* that was touched (text /
content-desc / resource-id) lives in <output_dir>/events/event_<tag>.json.

Both sides compute `event_str` with the same function against the same state,
so (start_state, stop_state, event_str) is an exact join key between them.
Joining gives a graph whose edges know both the structure and the selector,
which is everything needed to emit a test deterministically.
"""
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .menu_tree import MenuState, MenuTree, Selector, Transition

logger = logging.getLogger(__name__)

# utg.js is a JS assignment, not JSON: `var utg = \n{...}`
_UTG_PREFIX = re.compile(r"^\s*var\s+utg\s*=\s*", re.IGNORECASE)

DEFAULT_SELECTOR_PRIORITY = ("text", "content_description", "resource_id")

_STRATEGY_BY_KEY = {
    "text": "text",
    "content_description": "desc",
    "resource_id": "resourceId",
}

_UNMATCHED_HINT = (
    "Most likely cause: DroidBot names event files with second-resolution "
    "timestamps, so two events in the same second overwrite each other."
)


class UTGParseError(Exception):
    pass


class UTGParser:
    def __init__(self, config: dict):
        self.output_dir = Path(config["output_dir"])
        self.utg_file = self.output_dir / config.get("utg_file", "utg.js")
        self.events_dir = self.output_dir / config.get("events_subdir", "events")
        self.states_dir = self.output_dir / config.get("states_subdir", "states")
        self.fallback_to_class = config.get("fallback_to_class", True)
        self.selector_priority = tuple(
            config.get("selector_priority", DEFAULT_SELECTOR_PRIORITY)
        )
        self.strict = config.get("strict", True)
        # Jetpack Compose renders clickable wrappers with no text/desc/id of
        # their own; the label sits on a descendant node. Resolve through it.
        self.resolve_descendant_labels = config.get("resolve_descendant_labels", True)
        self.max_descendant_depth = int(config.get("max_descendant_depth", 4))
        self._state_views: Dict[str, List[dict]] = {}
        self._descendant_resolved = 0

    # -- public ----------------------------------------------------------
    def parse(self) -> MenuTree:
        utg = self._load_utg()
        self._state_views = self._index_states()
        event_index = self._index_events()

        tree = MenuTree(meta=self._extract_meta(utg))
        self._load_states(utg, tree)
        self._load_transitions(utg, tree, event_index)
        self._prune_foreign_states(tree, utg.get("app_package"))
        self._dedupe_transitions(tree)
        tree.root = self._find_root(utg, tree)
        tree.invalidate_cache()

        if tree.root is None:
            self._fail("Could not determine the root state of the UTG.")

        if self._descendant_resolved:
            logger.info(
                "Resolved %d selector(s) from a descendant node (Compose-style "
                "clickable wrappers with the label on a child).",
                self._descendant_resolved,
            )
        ambiguous = tree.ambiguous_transitions()
        if ambiguous:
            self._warn(
                f"{len(ambiguous)} transition(s) resolved only to a className "
                "selector, which cannot uniquely identify a control. They are "
                "reported as coverage gaps rather than emitted as tests."
            )
        logger.info("MenuTree reconstructed: %s", tree.summary())
        return tree

    # -- utg.js ----------------------------------------------------------
    def _load_utg(self) -> dict:
        if not self.utg_file.exists():
            self._fail(
                f"utg.js not found at {self.utg_file}. "
                "The crawl did not complete, or output_dir is wrong."
            )
            return {}
        raw = self.utg_file.read_text(encoding="utf-8")
        stripped = _UTG_PREFIX.sub("", raw, count=1).strip().rstrip(";")
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            self._fail(
                f"utg.js is not parseable JSON after stripping the var prefix: {exc}"
            )
            return {}

    def _extract_meta(self, utg: dict) -> dict:
        keys = (
            "app_package", "app_main_activity", "app_sha256",
            "app_num_total_activities", "num_nodes", "num_edges",
            "num_effective_events", "num_reached_activities", "num_transitions",
            "test_date", "time_spent", "device_serial",
            "device_model_number", "device_sdk_version",
        )
        return {k: utg.get(k) for k in keys if k in utg}

    def _load_states(self, utg: dict, tree: MenuTree) -> None:
        for node in utg.get("nodes", []):
            state_str = node.get("id") or node.get("state_str")
            if not state_str:
                self._warn("UTG node without an id; skipping.")
                continue
            label = (node.get("label") or "")
            tree.states[state_str] = MenuState(
                state_str=state_str,
                activity=node.get("activity", ""),
                package=node.get("package", ""),
                structure_str=node.get("structure_str", ""),
                screenshot=node.get("image"),
                label=label.replace("\n<FIRST>", "").replace("\n<LAST>", ""),
            )

    def _load_transitions(
        self, utg: dict, tree: MenuTree, event_index: dict
    ) -> None:
        unmatched = 0
        for edge in utg.get("edges", []):
            from_state = edge.get("from")
            to_state = edge.get("to")
            if not from_state or not to_state:
                self._warn("UTG edge missing from/to; skipping.")
                continue

            for event in edge.get("events", []):
                event_str = event.get("event_str", "")
                event_type = (event.get("event_type") or "").lower()

                detail = event_index.get((from_state, to_state, event_str))
                if detail is None:
                    # Weaker fallback: match on event_str alone.
                    detail = event_index.get((None, None, event_str))
                if detail is None:
                    unmatched += 1

                detail = detail or {}
                tree.transitions.append(
                    Transition(
                        from_state=from_state,
                        to_state=to_state,
                        event_id=int(event.get("event_id", -1)),
                        event_type=event_type,
                        event_str=event_str,
                        selector=self._selector_from_view(
                            detail.get("view"), from_state
                        ),
                        input_text=detail.get("text"),
                        key_name=detail.get("name"),
                    )
                )

        if unmatched:
            self._warn(
                f"{unmatched} UTG edge event(s) had no matching events/*.json "
                "record; those edges have no selector and cannot become "
                f"testcases. {_UNMATCHED_HINT}"
            )

    def _find_root(self, utg: dict, tree: MenuTree) -> Optional[str]:
        """The app's entry state -- where a `launch` lands.

        NOT DroidBot's <FIRST> state: that is whatever happened to be on screen
        when the crawl began, usually the launcher home screen. The only edge
        out of it is the launch intent, which paths must never replay because
        every emitted testcase already starts with an explicit `launch`.
        """
        app_package = utg.get("app_package")

        def in_app(state_str: str) -> bool:
            state = tree.states.get(state_str)
            if state is None:
                return False
            return not app_package or state.package == app_package

        # 1. Target of the earliest launch/restart edge that lands in the app.
        launch_targets = [
            t for t in tree.transitions if t.is_restart and in_app(t.to_state)
        ]
        if launch_targets:
            return min(launch_targets, key=lambda t: t.event_id).to_state

        # 2. Source of the earliest in-app edge -- where exploration began
        #    once the app was actually on screen.
        in_app_edges = [t for t in tree.transitions if in_app(t.from_state)]
        if in_app_edges:
            return min(in_app_edges, key=lambda t: t.event_id).from_state

        # 3. A state on the manifest main activity.
        main_activity = utg.get("app_main_activity")
        if main_activity:
            for state_str, state in tree.states.items():
                if state.activity == main_activity and in_app(state_str):
                    return state_str

        # 4. DroidBot's <FIRST>, but only if it belongs to the app.
        for node in utg.get("nodes", []):
            if "<FIRST>" in (node.get("label") or "") and in_app(node.get("id", "")):
                return node.get("id")
        return None

    @staticmethod
    def _dedupe_transitions(tree: MenuTree) -> None:
        """Collapse edges that represent the same user action.

        DroidBot re-records an event every time DFS revisits it, so the same
        (source, target, control) triple appears repeatedly. Emitting one
        testcase per duplicate produces byte-identical tests under different
        names, inflating the count without adding coverage.
        """
        seen = {}
        for transition in sorted(tree.transitions, key=lambda t: t.event_id):
            key = (
                transition.from_state,
                transition.to_state,
                transition.event_type,
                transition.selector,
                transition.key_name,
            )
            seen.setdefault(key, transition)
        removed = len(tree.transitions) - len(seen)
        if removed:
            tree.transitions = list(seen.values())
            tree.invalidate_cache()
            logger.info(
                "Collapsed %d duplicate transition(s) (same control, same "
                "source and target, re-walked by DFS).",
                removed,
            )

    def _prune_foreign_states(self, tree: MenuTree, app_package: Optional[str]) -> None:
        """Drop launcher/system states so they cannot pollute app coverage.

        Two kinds are removed:

        1. States whose foreground activity belongs to another package (the
           launcher home screen, system dialogs DroidBot wandered into).
        2. States that *claim* the app's activity but whose view hierarchy
           contains nothing from the app. These are launch-transition frames:
           ActivityManager has already switched to the app, but the screen is
           still showing the launcher. Left in, they become the graph root and
           prefix every generated testcase with a bogus launcher click.
        """
        if not app_package:
            return

        foreign = set()
        transitional = set()
        for state_str, state in tree.states.items():
            if state.package != app_package:
                foreign.add(state_str)
                continue
            views = self._state_views.get(state_str)
            if views and not any(v.get("package") == app_package for v in views):
                transitional.add(state_str)

        if transitional:
            self._warn(
                f"{len(transitional)} state(s) report the app's activity but "
                "contain no views from the app -- launch-transition frames. "
                "Pruning them so they cannot become the graph root."
            )
        foreign |= transitional
        if not foreign:
            return
        for state_str in foreign:
            tree.states.pop(state_str, None)
        before = len(tree.transitions)
        tree.transitions = [
            t
            for t in tree.transitions
            if t.from_state not in foreign and t.to_state not in foreign
        ]
        tree.invalidate_cache()
        logger.info(
            "Pruned %d out-of-app state(s) and %d edge(s) (launcher/system screens).",
            len(foreign),
            before - len(tree.transitions),
        )

    # -- events/*.json ---------------------------------------------------
    def _index_events(self) -> Dict[Tuple[Optional[str], Optional[str], str], dict]:
        """Index every saved event by (start_state, stop_state, event_str).

        Also stores an (None, None, event_str) alias as a weaker fallback.
        """
        index: Dict[Tuple[Optional[str], Optional[str], str], dict] = {}
        if not self.events_dir.exists():
            self._warn(
                f"Events directory {self.events_dir} not found. Transitions will "
                "have no selectors, so no testcases can be emitted."
            )
            return index

        files = sorted(self.events_dir.glob("event_*.json"))
        malformed: List[str] = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                # Never silently swallow: a partial parse means a partial gate.
                malformed.append(f"{path.name}: {exc}")
                continue

            event = data.get("event", {})
            record = {
                "view": event.get("view"),
                "text": event.get("text"),
                "name": event.get("name"),
                "event_type": event.get("event_type"),
            }
            event_str = data.get("event_str", "")
            index[(data.get("start_state"), data.get("stop_state"), event_str)] = record
            index.setdefault((None, None, event_str), record)

        if malformed:
            preview = "; ".join(malformed[:5])
            suffix = "..." if len(malformed) > 5 else ""
            self._warn(
                f"{len(malformed)} event file(s) could not be read: {preview}{suffix}"
            )
        logger.info("Indexed %d event file(s) from %s", len(files), self.events_dir)
        return index

    # -- states/*.json ---------------------------------------------------
    def _index_states(self) -> Dict[str, List[dict]]:
        """Index each captured state's full view list by its state_str.

        Needed to resolve labels through the view tree: an event only carries
        the view that was touched, but a Compose clickable's label lives on a
        descendant node that only the full state hierarchy contains.
        """
        index: Dict[str, List[dict]] = {}
        if not self.states_dir.exists():
            return index

        for path in sorted(self.states_dir.glob("state_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                self._warn(f"State file {path.name} could not be read: {exc}")
                continue
            state_str = data.get("state_str")
            if state_str:
                index[state_str] = data.get("views", [])
        logger.info("Indexed %d state hierarchy/hierarchies", len(index))
        return index

    # -- selectors -------------------------------------------------------
    def _direct_identifier(self, view: dict) -> Optional[Selector]:
        for key in self.selector_priority:
            value = view.get(key)
            if value and str(value).strip():
                value = str(value).strip()
                if key == "resource_id" and "/" in value:
                    value = value.split("/")[-1]
                return Selector(_STRATEGY_BY_KEY.get(key, key), value)
        return None

    def _descendant_identifier(
        self, view: dict, state_str: Optional[str]
    ) -> Optional[Selector]:
        """Breadth-first search the touched view's subtree for a label.

        Jetpack Compose emits a bare clickable `android.view.View` whose
        content-description sits on a child node. Without this, every Compose
        control collapses to the useless selector `className "View"`.
        """
        views = self._state_views.get(state_str or "")
        if not views:
            return None

        queue: List[Tuple[int, int]] = [
            (child, 1) for child in view.get("children", []) or []
        ]
        while queue:
            index, depth = queue.pop(0)
            if not isinstance(index, int) or not 0 <= index < len(views):
                continue
            if depth > self.max_descendant_depth:
                continue
            child = views[index]
            found = self._direct_identifier(child)
            if found:
                return found
            queue.extend((c, depth + 1) for c in child.get("children", []) or [])
        return None

    def _selector_from_view(
        self, view: Optional[dict], state_str: Optional[str] = None
    ) -> Optional[Selector]:
        if not view:
            return None

        direct = self._direct_identifier(view)
        if direct:
            return direct

        if self.resolve_descendant_labels:
            inherited = self._descendant_identifier(view, state_str)
            if inherited:
                self._descendant_resolved += 1
                return inherited

        if self.fallback_to_class and view.get("class"):
            return Selector("className", str(view["class"]).split(".")[-1])
        return None

    # -- diagnostics -----------------------------------------------------
    def _warn(self, message: str) -> None:
        logger.warning(message)

    def _fail(self, message: str) -> None:
        if self.strict:
            raise UTGParseError(message)
        logger.error(message)
