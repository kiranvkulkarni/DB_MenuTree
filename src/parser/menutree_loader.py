"""Load a menutree.json (from the replay explorer) into a MenuTree.

The replay explorer already resolves selectors and state identity at capture
time, so this is a straight deserialisation -- none of the reconstruction and
repair the DroidBot path needs. Everything downstream (path emission,
coverage, the gate) is unchanged.
"""
import json
import logging
from pathlib import Path
from typing import Optional

from .menu_tree import MenuState, MenuTree, Selector, Transition

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = {"menutree/1"}

_ACTION_TO_EVENT_TYPE = {
    "click": "touch",
    "long_click": "long_touch",
}


class MenuTreeLoadError(Exception):
    pass


class MenuTreeLoader:
    def __init__(self, config: dict):
        output_dir = Path(config.get("output_dir", "./u2_out"))
        self.graph_file = Path(
            config.get("graph_file") or (output_dir / "menutree.json")
        )

    def load(self) -> MenuTree:
        if not self.graph_file.exists():
            raise MenuTreeLoadError(
                f"Graph file not found: {self.graph_file}. Run the explorer first."
            )
        try:
            data = json.loads(self.graph_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise MenuTreeLoadError(f"{self.graph_file} is unreadable: {exc}") from exc

        fmt = data.get("format")
        if fmt not in SUPPORTED_FORMATS:
            raise MenuTreeLoadError(
                f"Unsupported graph format '{fmt}'; expected one of "
                f"{sorted(SUPPORTED_FORMATS)}"
            )

        package = data.get("app_package", "")
        tree = MenuTree(
            root=data.get("root"),
            meta={
                "app_package": package,
                "app_main_activity": None,
                **(data.get("stats") or {}),
            },
        )

        for entry in data.get("states", []):
            key = entry.get("key")
            if not key:
                continue
            tree.states[key] = MenuState(
                state_str=key,
                activity=entry.get("activity") or package,
                package=entry.get("package") or package,
                structure_str=key,
                screenshot=entry.get("screenshot"),
                label=f"{(entry.get('activity') or package).split('.')[-1]}",
            )

        for entry in data.get("edges", []):
            from_state = entry.get("from")
            to_state = entry.get("to")
            if not from_state or not to_state:
                continue
            strategy = entry.get("selector_strategy")
            value = entry.get("selector_value")
            tree.transitions.append(
                Transition(
                    from_state=from_state,
                    to_state=to_state,
                    event_id=int(entry.get("order", 0)),
                    event_type=_ACTION_TO_EVENT_TYPE.get(
                        entry.get("action", "click"), "touch"
                    ),
                    event_str=f"{entry.get('action')}({strategy}={value})",
                    selector=Selector(strategy, value) if strategy and value else None,
                )
            )

        tree.invalidate_cache()
        if tree.root not in tree.states:
            raise MenuTreeLoadError(
                f"Graph root '{tree.root}' is not among its states."
            )
        logger.info("MenuTree loaded from %s: %s", self.graph_file, tree.summary())
        return tree
