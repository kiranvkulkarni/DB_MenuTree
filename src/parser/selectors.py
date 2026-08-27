"""Resolve a view into a test selector.

Shared by both crawler back-ends. Every view is normalised to the same dict
shape first -- keys `text`, `content_description`, `resource_id`, `class`,
`children` (indices into the state's view list) -- so this logic is written
once, regardless of which back-end produced the hierarchy.
"""
from typing import Dict, List, Optional, Sequence, Tuple

from .menu_tree import Selector

# Confirmed selector preference, strongest first:
#   1. text   2. description   3. resource id   4. xpath
# xpath is built structurally and is always available, so it is the final
# fallback rather than one of the priority keys.
DEFAULT_SELECTOR_PRIORITY: Tuple[str, ...] = (
    "text",
    "content_description",
    "resource_id",
)

# Keys are view-dict fields; values are UVTA selector keywords, per the
# DSL cheat sheet ("Selector Types"). These strings go straight into emitted
# commands, so they must match the DSL exactly.
STRATEGY_BY_KEY = {
    "text": "text",
    "content_description": "desc",
    "resource_id": "id",
}


class SelectorResolver:
    def __init__(
        self,
        priority: Sequence[str] = DEFAULT_SELECTOR_PRIORITY,
        fallback_to_class: bool = True,
        resolve_descendants: bool = True,
        max_descendant_depth: int = 4,
        max_xpath_depth: int = 40,
    ):
        self.priority = tuple(priority)
        self.fallback_to_class = fallback_to_class
        self.resolve_descendants = resolve_descendants
        self.max_descendant_depth = max_descendant_depth
        self.max_xpath_depth = max_xpath_depth
        self.descendant_hits = 0
        self.xpath_hits = 0

    def direct(self, view: Dict) -> Optional[Selector]:
        for key in self.priority:
            value = view.get(key)
            if value and str(value).strip():
                value = str(value).strip()
                if key == "resource_id" and "/" in value:
                    value = value.split("/")[-1]
                return Selector(STRATEGY_BY_KEY.get(key, key), value)
        return None

    def descendant(
        self, view: Dict, views: Optional[List[Dict]]
    ) -> Optional[Selector]:
        """Breadth-first search the view's subtree for a usable label.

        Jetpack Compose emits a bare clickable `android.view.View` whose
        content-description sits on a child node. Without this, every Compose
        control collapses to the useless selector `class "View"`.
        """
        if not views:
            return None

        queue: List[Tuple[int, int]] = [
            (child, 1) for child in (view.get("children") or [])
        ]
        while queue:
            index, depth = queue.pop(0)
            if not isinstance(index, int) or not 0 <= index < len(views):
                continue
            if depth > self.max_descendant_depth:
                continue
            child = views[index]
            found = self.direct(child)
            if found:
                return found
            queue.extend((c, depth + 1) for c in (child.get("children") or []))
        return None

    def xpath(self, view: Dict, views: Optional[List[Dict]]) -> Optional[Selector]:
        """Absolute structural XPath, the last-resort selector.

        Built from class names plus the node's index among same-class
        siblings, e.g.

            /android.widget.FrameLayout[1]/android.view.View[2]/...

        Always constructible, and unique -- which is why it replaces the old
        `class "View"` fallback, where a Compose screen full of bare views
        made that selector match anything.

        The trade-off is fragility: an XPath encodes layout, so it breaks
        when the layout shifts even if the control is unchanged. For a gate
        that compares builds, an XPath-selected row is the one most likely to
        report a false failure, so these are counted separately.
        """
        if not views:
            return None

        chain: List[str] = []
        node: Optional[Dict] = view
        guard = 0
        while node is not None and guard <= self.max_xpath_depth:
            guard += 1
            cls = node.get("class") or "*"
            parent_index = node.get("parent")
            if parent_index is None or not (0 <= parent_index < len(views)):
                chain.append(f"/{cls}")
                break
            parent = views[parent_index]
            siblings = [
                views[c] for c in (parent.get("children") or [])
                if isinstance(c, int) and 0 <= c < len(views)
            ]
            same_class = [s for s in siblings if (s.get("class") or "*") == cls]
            position = 1
            for index, sibling in enumerate(same_class, start=1):
                if sibling.get("temp_id") == node.get("temp_id"):
                    position = index
                    break
            chain.append(f"/{cls}[{position}]")
            node = parent

        if not chain:
            return None
        return Selector("xpath", "".join(reversed(chain)))

    def resolve(
        self, view: Optional[Dict], views: Optional[List[Dict]] = None
    ) -> Optional[Selector]:
        if not view:
            return None

        found = self.direct(view)
        if found:
            return found

        if self.resolve_descendants:
            found = self.descendant(view, views)
            if found:
                self.descendant_hits += 1
                return found

        found = self.xpath(view, views)
        if found:
            self.xpath_hits += 1
            return found

        if self.fallback_to_class and view.get("class"):
            return Selector("class", str(view["class"]).split(".")[-1])
        return None
