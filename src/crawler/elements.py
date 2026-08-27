"""Enumerate every UI element on a screen, not just the clickable ones.

The MenuTree deliverable is a tree of *elements* laid out by depth — one row
per item — not a graph of screens. A filter list contributes one row per
filter (`Original`, `Classic film`, `Crystal`, …); a settings page
contributes rows for its title and subtitles as well as its controls.

A state-graph crawler collapses all of that into a single node, which is why
graph-based output looks so much shallower than the expected sheet. This
module produces the element-level view instead.

Kinds are inferred structurally from class and properties. They approximate
the annotations a human writes in the sheet (`[Title]`, `(On/Off)`,
`(Radio button On/Off)`); they do not reproduce editorial notes such as
`[When location Permission is OFF in DUT]`, which are specification
knowledge rather than anything observable on screen.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..parser.selectors import SelectorResolver

# Content-descriptions Android uses for up/back affordances.
_BACK_HINTS = ("navigate up", "back", "go back", "close", "dismiss")

_TOGGLE_HINTS = ("switch", "checkbox", "togglebutton", "switchcompat")
_RADIO_HINTS = ("radiobutton",)
_EDIT_HINTS = ("edittext",)

# Never part of an app's menu tree. The IME is resolved at runtime too
# (see ElementTreeWalker), since the active keyboard varies by device --
# Gboard on Pixel, Honeyboard on Samsung.
CHROME_PACKAGES = (
    "com.android.systemui",
    "com.google.android.inputmethod.latin",
    "com.samsung.android.honeyboard",
    "com.touchtype.swiftkey",
)


@dataclass
class Element:
    """One row of the MenuTree."""
    label: str
    kind: str            # title | subtitle | text | button | toggle | radio | input | back | item
    interactive: bool
    view_index: int
    resource_id: Optional[str] = None
    checked: Optional[bool] = None
    selected: bool = False
    bounds: str = ""
    # How a test should address this element, resolved from the XML dump in
    # the confirmed order text -> description -> resource id -> xpath. The
    # label alone is not enough: it is whichever of text or content-desc was
    # non-empty, so emitting `text "Flash"` for an icon whose label came from
    # its content-desc produces a selector that cannot match at runtime.
    selector_kind: Optional[str] = None    # text | desc | id | xpath | class
    selector_value: Optional[str] = None

    def annotated(self) -> str:
        """Label with a type annotation, in the sheet's style."""
        if self.kind == "title":
            return f"{self.label} [Title]"
        if self.kind == "subtitle":
            return f"{self.label} [subtitle]"
        if self.kind == "toggle":
            return f"{self.label} (On/Off)"
        if self.kind == "radio":
            return f"{self.label} (Radio button On/Off)"
        return self.label


def _visible_label(view: Dict) -> Optional[str]:
    """The text a user actually sees. Never a resource-id."""
    for key in ("text", "content_description"):
        value = view.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


def _fallback_label(view: Dict) -> Optional[str]:
    """Resource-id, for controls a user can press but that carry no label."""
    rid = view.get("resource_id")
    return str(rid).split("/")[-1] if rid else None


def _kind_of(view: Dict, label: str, is_first_text: bool) -> str:
    cls = (view.get("class") or "").lower()
    desc = (view.get("content_description") or "").lower()
    clickable = bool(view.get("clickable") or view.get("long_clickable"))

    if any(h in cls for h in _EDIT_HINTS) or view.get("editable"):
        return "input"
    if any(h in cls for h in _TOGGLE_HINTS) or view.get("checkable"):
        return "toggle"
    if any(h in cls for h in _RADIO_HINTS):
        return "radio"
    if clickable and any(h == desc or h in desc for h in _BACK_HINTS):
        return "back"
    if clickable:
        return "button"
    # Non-interactive text: the first one on a screen reads as its title.
    if is_first_text:
        return "title"
    return "text"


# Resolves each view into a selector, in the confirmed order:
#   1. text   2. description   3. resource id   4. xpath
#
# Which of these a control offers is entirely up to whoever built it -- some
# expose text, some only a content-desc, some only an id, and some nothing at
# all, which is what the structural xpath fallback is for. Reading it off the
# XML dump per element is the only way to know.
_RESOLVER = SelectorResolver()


def enumerate_elements(
    views: Sequence[Dict],
    package: Optional[str] = None,
    include_static_text: bool = True,
    include_foreign: bool = True,
    exclude_packages: Sequence[str] = CHROME_PACKAGES,
) -> List[Element]:
    """Every labelled element on screen, in document (top-to-bottom) order.

    `include_foreign` keeps system-package elements such as the permission
    dialog's `Precise` / `Approximate` / `Only this time`. Those belong to
    `com.android.permissioncontroller`, not the app, but they are part of the
    app's flow and appear in the expected sheet, so excluding them loses real
    coverage.
    """
    elements: List[Element] = []
    seen_text = False

    for index, view in enumerate(views):
        view_package = view.get("package")
        if view_package and package and view_package != package:
            if not include_foreign:
                continue
            # Status bar, navigation bar and the on-screen keyboard are
            # not part of the app's menu tree. Left in, a single tap into a
            # search field adds a row per key: q, w, e, 1, 2 ...
            if any(view_package.startswith(x) for x in exclude_packages):
                continue

        interactive = bool(
            view.get("clickable")
            or view.get("long_clickable")
            or view.get("checkable")
            or view.get("editable")
        )

        label = _visible_label(view)
        if not label:
            # No visible label. An unlabelled *control* still deserves a row
            # -- the user can press it -- so fall back to its resource-id.
            # An unlabelled non-interactive view is a layout container
            # (action_bar_root, main_screen_coordinator_layout, ...) and is
            # not a menu item. Emitting those buries the real tree: they were
            # 60% of a first run's rows.
            if not interactive:
                continue
            label = _fallback_label(view)
            if not label:
                continue

        if not interactive and not include_static_text:
            continue

        kind = _kind_of(view, label, is_first_text=not interactive and not seen_text)
        if kind in ("title", "text", "subtitle"):
            seen_text = True

        selector = _RESOLVER.resolve(view, list(views))
        elements.append(
            Element(
                label=label,
                kind=kind,
                interactive=interactive,
                view_index=index,
                resource_id=(view.get("resource_id") or None),
                checked=view.get("checked") if view.get("checkable") else None,
                selected=bool(view.get("selected")),
                bounds=view.get("bounds", ""),
                selector_kind=selector.strategy if selector else None,
                selector_value=selector.value if selector else None,
            )
        )
    return elements


import re as _re

# Same state-suffix problem as element lookup, but for screen identity.
# A screen registered while the filter was off reads "filteroff"; revisit it
# with the filter on and it reads "filteron". Comparing exact labels drops
# similarity below the 0.75 threshold, so the walker fails to recognise a
# screen it already knows and pays for a relaunch. Measured: 54 of 60
# navigations unidentified, 88 relaunches consuming most of the budget.
_STATE_SUFFIX = _re.compile(r"(off|on|auto)$", _re.IGNORECASE)


def _stem(label: str) -> str:
    return _STATE_SUFFIX.sub("", (label or "").strip().lower()).strip()


def label_set(elements: Sequence[Element], normalise: bool = False) -> set:
    if normalise:
        return {_stem(e.label) for e in elements}
    return {e.label for e in elements}


def screen_similarity(
    before: Sequence[Element], after: Sequence[Element], normalise: bool = True
) -> float:
    """Jaccard overlap of two screens' labels.

    Used to tell "this click opened a submenu" from "this click merely
    selected an option". Choosing a filter or a resolution keeps you on the
    same screen with the same items, so those are leaves; opening Settings
    replaces the item set, so that descends.
    """
    a, b = label_set(before, normalise), label_set(after, normalise)
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)
