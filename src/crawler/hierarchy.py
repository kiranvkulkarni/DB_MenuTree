"""Parse a uiautomator hierarchy dump into normalised views and a state key.

Views are normalised to a single dict shape (`text`,
`content_description`, `resource_id`, `class`, `children`), so selector
resolution is shared between both crawler back-ends.

State abstraction
-----------------
The state key decides what counts as "the same screen", and it is the single
most consequential knob in the whole crawler:

  too coarse -> distinct screens merge and coverage silently under-reports
  too fine   -> volatile content explodes the graph and runs stop being
                reproducible

The default (`affordance`) hashes the tree's *structure* plus the text of
elements that participate in an affordance -- anything clickable/checkable,
or within `AFFORDANCE_TEXT_DEPTH` levels below one. Free-floating display
text is excluded, because values like a live gold rate or "Updated just now"
change between runs and would make every crawl discover a different graph.
"""
import hashlib
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence

# How far below a clickable ancestor text still counts as part of the affordance.
AFFORDANCE_TEXT_DEPTH = 3

_BOOL_ATTRS = (
    "checkable", "checked", "clickable", "enabled", "focusable",
    "scrollable", "selected", "password",
)

STATE_KEY_MODES = ("affordance", "structure", "content")

# Returned when a dump contains no views belonging to the target app.
EMPTY_STATE = "EMPTY"


def _as_bool(value: Optional[str]) -> bool:
    return str(value).lower() == "true"


def parse_hierarchy(xml_text: str) -> List[Dict]:
    """Flatten a uiautomator XML dump into an indexed list of view dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    views: List[Dict] = []

    def walk(element, parent_index: Optional[int]) -> Optional[int]:
        if element.tag != "node":
            index = None
        else:
            attrib = element.attrib
            cls = attrib.get("class", "")
            view = {
                "text": attrib.get("text") or None,
                "content_description": attrib.get("content-desc") or None,
                "resource_id": attrib.get("resource-id") or None,
                "class": cls,
                "package": attrib.get("package") or None,
                "long_clickable": _as_bool(attrib.get("long-clickable")),
                "editable": "EditText" in cls,
                "bounds": attrib.get("bounds", ""),
                "children": [],
                "parent": parent_index,
            }
            for name in _BOOL_ATTRS:
                view[name] = _as_bool(attrib.get(name))
            index = len(views)
            view["temp_id"] = index
            views.append(view)
            if parent_index is not None:
                views[parent_index]["children"].append(index)

        next_parent = index if index is not None else parent_index
        for child in element:
            walk(child, next_parent)
        return index

    walk(root, None)
    return views


def _affordance_depths(views: Sequence[Dict]) -> Dict[int, int]:
    """Distance from each view up to its nearest interactive ancestor."""
    depths: Dict[int, int] = {}
    for index, view in enumerate(views):
        depth = None
        current: Optional[int] = index
        hops = 0
        while current is not None and hops <= AFFORDANCE_TEXT_DEPTH:
            candidate = views[current]
            if (
                candidate.get("clickable")
                or candidate.get("checkable")
                or candidate.get("long_clickable")
                or candidate.get("editable")
            ):
                depth = hops
                break
            current = candidate.get("parent")
            hops += 1
        if depth is not None:
            depths[index] = depth
    return depths


_DEFAULT_CHROME = (
    "com.android.systemui",
    "com.google.android.inputmethod.latin",
    "com.samsung.android.honeyboard",
    "com.touchtype.swiftkey",
)


def state_key(
    views: Sequence[Dict],
    mode: str = "affordance",
    package: Optional[str] = None,
    exclude_packages: Sequence[str] = _DEFAULT_CHROME,
) -> str:
    """Stable hash identifying a screen.

    Excludes only known OS chrome (status bar, nav bar, IME) -- NOT every
    view outside `package`. A screen can legitimately be owned entirely by a
    system component: a runtime permission dialog is
    `com.google.android.permissioncontroller`, in full. The previous
    strict-to-`package` filter dropped every such view, so the "no content
    survived filtering" branch below fired -- EMPTY_STATE -- and the walker
    bailed out treating a real, addressable pop-up as nothing at all. `package`
    is kept as a parameter for compatibility but no longer used to filter.
    """
    if mode not in STATE_KEY_MODES:
        raise ValueError(f"unknown state key mode '{mode}'")

    affordances = _affordance_depths(views) if mode == "affordance" else {}
    parts: List[str] = []

    for index, view in enumerate(views):
        view_pkg = view.get("package")
        if view_pkg and any(view_pkg.startswith(x) for x in exclude_packages):
            continue

        fields = [
            view.get("class") or "",
            view.get("resource_id") or "",
            view.get("content_description") or "",
            "1" if view.get("clickable") else "0",
            "1" if view.get("checkable") else "0",
            "1" if view.get("checked") else "0",
            "1" if view.get("scrollable") else "0",
            "1" if view.get("editable") else "0",
            "1" if view.get("selected") else "0",
        ]

        if mode == "content":
            fields.append(view.get("text") or "")
        elif mode == "affordance" and index in affordances:
            # An editable field's text is user data, not a label. Including it
            # mints a new state for every keystroke or recomputed value: on the
            # target app, clicking fields holding "10.0" / "2.0" / a live rate
            # produced 8 spurious states. What identifies the screen is that
            # the field exists and is focused, not what it currently holds.
            if view.get("editable"):
                fields.append("<editable>")
            else:
                fields.append(view.get("text") or "")

        parts.append("|".join(fields))

    if not parts:
        # Every view was filtered out, so this dump shows none of the app -- a
        # launch-transition frame, a system dialog, or a screen captured before
        # the app rendered. Hashing it gives sha256("") for *every* such dump,
        # silently merging them into one bogus state, so report it as unusable.
        return EMPTY_STATE

    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def interactive_views(
    views: Sequence[Dict], package: Optional[str] = None
) -> List[int]:
    """Indices of views worth acting on, in deterministic document order."""
    found: List[int] = []
    for index, view in enumerate(views):
        if package and view.get("package") and view["package"] != package:
            continue
        if not view.get("enabled", True):
            continue
        if (
            view.get("clickable")
            or view.get("checkable")
            or view.get("long_clickable")
            or view.get("editable")
            or view.get("scrollable")
        ):
            found.append(index)
    return found


# Classes Android uses for modal surfaces. A dialog is a decision point: it
# usually appears once, so branches not taken while it is on screen may never
# become reachable again.
_DIALOG_CLASS_HINTS = ("alertdialog", "dialog", "popupwindow", "bottomsheet")


def scrollable_container(views: Sequence[Dict]) -> Optional[Dict]:
    """The tallest scrollable view on screen, or None."""
    best, best_height = None, 0
    for view in views:
        if not view.get("scrollable"):
            continue
        box = box_of(view)
        if not box:
            continue
        height = box[3] - box[1]
        if height >= best_height:
            best, best_height = view, height
    return best


def swipe_span(container: Dict, width: int, height: int
               ) -> Optional[tuple]:
    """Where to start and end a swipe inside this container: (x, low, high).

    Derived from the container's own box rather than a fixed pixel span. A
    fixed span assumed the list was tall and centred; on a short container
    near the top of the screen it produced a negative coordinate, which
    uiautomator2 rejects outright and which killed a two-hour run.

    Shared by the walker and the verifier so that fix lives in one place.
    """
    box = box_of(container)
    if not box:
        return None
    x1, y1, x2, y2 = box
    x = max(1, min(width - 2, (x1 + x2) // 2))
    # Keep clear of the edges: a swipe starting on the boundary can be taken
    # for a system gesture rather than a scroll.
    inset = max(8, (y2 - y1) // 10)
    low = max(1, min(height - 2, y1 + inset))
    high = max(1, min(height - 2, y2 - inset))
    if high - low < 40:
        return None                    # too short to scroll meaningfully
    return x, low, high


def foreground_package(
    views: Sequence[Dict], exclude_packages: Sequence[str] = _DEFAULT_CHROME
) -> str:
    """The app in front, read from a dump we have already paid for.

    `adb`/`u2`'s own "what is the current package" call costs ~390ms on the
    Realme against ~150ms for the whole hierarchy dump, and the quiescence
    loop needs it constantly. But the dump already carries a `package` on
    every node, and uiautomator dumps the *focused* window, so the package
    owning the most views is the foreground app.

    OS chrome is excluded because the status and navigation bars appear on
    every screen and would otherwise win on a sparse one.

    Measured against the authoritative call on the Realme camera: 24 of 24
    screens agreed once the screen had settled. Before settling it can lag a
    transition by one frame (a dump still showing the camera while the
    Gallery is coming up), so callers must resolve the package *after*
    quiescence, and should confirm a foreign answer with the real call
    before acting on it.
    """
    counts: Dict[str, int] = {}
    for view in views:
        pkg = view.get("package")
        if pkg and not any(pkg.startswith(x) for x in exclude_packages):
            counts[pkg] = counts.get(pkg, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


# How to get out of a modal that is in the way, in order of preference.
#
# Non-committal first: every one of these DECLINES whatever is being offered,
# so pressing one cannot enable a setting, accept a term or grant a
# permission. A walk that clears dialogs by pressing the affirmative branch
# changes the device it is measuring.
DECLINE_LABELS = ("cancel", "not now", "no thanks", "later", "deny",
                  "don't allow", "dont allow", "skip")

# An informational dialog often has no way out but acknowledgement --
# "Location tags and sharing ... OK" had no non-committal option at all, and
# stranded 53 of 60 rows behind it while the run relaunched 52 times into the
# same wall. Tried only after the declines and after BACK, and the action
# guard still applies, so an "OK" that would confirm something destructive is
# refused before it is pressed.
ACKNOWLEDGE_LABELS = ("ok", "got it", "dismiss", "close", "continue", "done")


def pick_dismissal(labels: Sequence[str], elements: Sequence,
                   blocked=None) -> Optional[object]:
    """The element to press to get out of a modal, or None.

    One place decides which button clears a dialog, because both the walker
    and the verifier have to make this choice and they must not drift apart:
    a discovery run that declines a prompt and a verification run that
    accepts it are measuring two different devices.

    Priority is `labels` order, not screen order. That matters -- "Cancel"
    and "Turn on" sit side by side and document order would pick whichever
    the layout happened to place first, which on a right-to-left locale is
    the affirmative one.

    `blocked(label) -> reason or falsy` lets the action guard veto a press
    before it happens, so an "OK" that would confirm something destructive is
    skipped and the next candidate tried instead.
    """
    for wanted in labels:
        for element in elements:
            if getattr(element, "label", "").strip().lower() != wanted:
                continue
            if blocked and blocked(element.label):
                continue
            return element
    return None


def looks_like_dialog(views: Sequence[Dict], package: Optional[str] = None) -> bool:
    """Heuristic: is this screen a modal decision point?

    Detected structurally, not semantically -- no model needed. Two signals:
    an explicit dialog class anywhere in the tree, or a small view count with
    few clickables (a modal shows far less than a full screen).

    Deliberately NOT filtered to `package`-owned views: a runtime permission
    prompt ("Allow Camera to access this device's location?") is owned
    *entirely* by com.google.android.permissioncontroller, none of it by the
    app. Filtering to `package` first emptied the view list before the
    heuristic ever ran, so this always returned False for exactly the
    dialogs it exists to catch. `package` is accepted for signature
    compatibility but no longer changes the result; only OS chrome (status
    bar, nav bar, IME) is excluded from the count.
    """
    content = [
        v for v in views
        if not (v.get("package") or "").startswith(_DEFAULT_CHROME)
    ]
    if not content:
        return False

    for view in content:
        cls = (view.get("class") or "").lower()
        rid = (view.get("resource_id") or "").lower()
        if any(hint in cls or hint in rid for hint in _DIALOG_CLASS_HINTS):
            return True

    # A modal is small. Full app screens observed here run 80-150 views;
    # the Samsung location-tags dialog was 35.
    clickable = sum(1 for v in content if v.get("clickable"))
    return len(content) <= 45 and 1 < clickable <= 6


def box_of(view: Dict) -> Optional[tuple]:
    """Parse a `[x1,y1][x2,y2]` bounds string into (x1, y1, x2, y2)."""
    bounds = view.get("bounds") or ""
    try:
        first, second = bounds.split("][")
        x1, y1 = (int(v) for v in first.lstrip("[").split(","))
        x2, y2 = (int(v) for v in second.rstrip("]").split(","))
    except (ValueError, AttributeError):
        return None
    return (x1, y1, x2, y2)


def center_of(view: Dict) -> Optional[tuple]:
    """Parse a `[x1,y1][x2,y2]` bounds string into a centre point."""
    bounds = view.get("bounds") or ""
    try:
        first, second = bounds.split("][")
        x1, y1 = (int(v) for v in first.lstrip("[").split(","))
        x2, y2 = (int(v) for v in second.rstrip("]").split(","))
    except (ValueError, AttributeError):
        return None
    return ((x1 + x2) // 2, (y1 + y2) // 2)
