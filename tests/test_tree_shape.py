"""Offline checks for the shape of the delivered tree.

Reported from the generated workbook, by comparing it against a hand-written
"expected" sheet. Two faults, both in how the tree is shaped after the walk
rather than in the walk itself:

* **Depth was the route taken, not the route that exists.** Any click that
  changed the screen counted as a descent, so lateral moves -- switching
  lens, PHOTO to VIDEO -- pushed everything deeper. One run reached Settings
  as `Filters > Motion photo > Blanc > Switch to front camera > VIDEO >
  Switch to rear camera > Quick controls > Go to Settings` and listed its
  contents at depth 10. It is two clicks from the viewfinder.

* **Rows came out in discovery order.** Every element of one screen, then
  every element of the next, so a parent was separated from its children by
  the whole rest of its own screen. A MenuTree has to read as a tree.

    python tests/test_tree_shape.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.element_tree import ElementTreeWalker, TreeNode  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def node(label, screen, depth=2, path=None):
    return TreeNode(label=label, raw_label=label, kind="button", depth=depth,
                    path=list(path or []), screen_key=screen)


def shaper(rows, edges, root="root"):
    w = ElementTreeWalker.__new__(ElementTreeWalker)
    w.rows, w._edges, w._root_key = rows, edges, root
    w._reparented = w._orphan_screens = 0
    return w


def main() -> int:
    ok = True

    print("a screen is placed on the shortest route that reaches it")
    # Settings was found the long way round, and also sits one click from the
    # viewfinder. The short route is the one a person would document.
    rows = [
        node("Quick controls", "root"),
        node("Go to Settings", "root"),
        node("Scanning", "settings", 10,
             ["Filters", "Motion photo", "Blanc", "VIDEO", "Quick controls",
              "Go to Settings"]),
    ]
    edges = {("root", "Go to Settings"): "settings",
             ("long", "Go to Settings"): "settings"}
    out = shaper(rows, edges)._shape_tree()
    scanning = [r for r in out if r.label == "Scanning"][0]
    ok &= check("depth 10 becomes depth 3", scanning.depth == 3,
                f"depth {scanning.depth}")
    ok &= check("the wandering path is replaced by the short one",
                scanning.path == ["Go to Settings"],
                " > ".join(scanning.path))

    print()
    print("rows come out in tree order, not discovery order")
    ok &= check("a parent is immediately followed by its subtree",
                [r.label for r in out] ==
                ["Quick controls", "Go to Settings", "Scanning"],
                " | ".join(r.label for r in out))

    print()
    print("a label's destination depends on where it is pressed")
    # The first attempt at this inferred edges from labels: if "Motion photo"
    # opens screen X somewhere, treat it as opening X everywhere. That
    # re-parented the viewfinder's "Motion photo" onto the Filters panel's,
    # and the whole filter list appeared under it.
    rows2 = [
        node("Motion photo", "root"),
        node("Filters", "root"),
        node("Motion photo", "filters", 3, ["Filters"]),
        node("Original", "filter_sub", 4, ["Filters", "Motion photo"]),
    ]
    edges2 = {("root", "Filters"): "filters",
              ("filters", "Motion photo"): "filter_sub"}
    out2 = shaper(rows2, edges2)._shape_tree()
    root_mp = [r for r in out2 if r.label == "Motion photo" and r.screen_key == "root"][0]
    original = [r for r in out2 if r.label == "Original"][0]
    ok &= check("the root's control is not given another screen's destination",
                root_mp.depth == 2 and root_mp.path == [])
    ok &= check("the subtree stays under the control that actually opens it",
                original.path == ["Filters", "Motion photo"],
                " > ".join(original.path))

    print()
    print("only observed edges are used")
    rows3 = [node("A", "root"), node("B", "unreached", 5, ["x", "y", "z"])]
    w = shaper(rows3, {})
    out3 = w._shape_tree()
    ok &= check("no route means the rows are kept, not dropped",
                len(out3) == 2, f"{len(out3)} rows")
    orphan = [r for r in out3 if r.label == "B"][0]
    ok &= check("and are marked rather than given an invented parent",
                "route" in orphan.note.lower() and orphan.path == ["x", "y", "z"],
                orphan.note)
    ok &= check("orphans are counted", w._orphan_screens == 1)

    print()
    print("a screen reachable two ways appears once, under the shorter")
    rows4 = [node("Short", "root"), node("Long", "root"),
             node("Mid", "mid", 3, ["Long"]),
             node("Leaf", "target", 4, ["Long", "Mid"])]
    edges4 = {("root", "Short"): "target", ("root", "Long"): "mid",
              ("mid", "Mid"): "target"}
    out4 = shaper(rows4, edges4)._shape_tree()
    leaf = [r for r in out4 if r.label == "Leaf"]
    ok &= check("listed once", len(leaf) == 1)
    ok &= check("under the one-click parent, not the two-click one",
                leaf[0].path == ["Short"], " > ".join(leaf[0].path))

    print()
    print("the package row survives")
    rows5 = [TreeNode(label="com.x", raw_label="com.x", kind="app", depth=1),
             node("A", "root")]
    out5 = shaper(rows5, {})._shape_tree()
    ok &= check("depth 1 stays at the top",
                out5[0].depth == 1 and len(out5) == 2)

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
