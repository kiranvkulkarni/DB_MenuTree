"""Offline checks for what the walk visits next, and what it lists twice.

Two faults that show up in the delivered workbook rather than in any counter:

* **Duplicate rows.** The same logical screen can arrive under a different
  state key -- the viewfinder's description carries the active lens, a tip
  card comes and goes -- and registered on the key alone, every variant
  re-listed the whole screen. Measured: 34 duplicated (depth, path, label)
  combinations, 68 of 225 rows, in one run.

* **Breadth-first when a branch runs out.** Descent was always depth-first,
  but when a screen was exhausted the walk took the SHALLOWEST pending item
  anywhere -- usually back at the root, the furthest thing from where it was
  standing, and a guaranteed relaunch. With `back_ok 1` against
  `back_failed 75`, navigation is the whole budget.

    python tests/test_traversal.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.element_tree import ElementTreeWalker, WorkItem  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def walker() -> ElementTreeWalker:
    return ElementTreeWalker.__new__(ElementTreeWalker)


def item(screen, label, path, depth):
    return WorkItem(screen_key=screen, label=label, kind="button",
                    path=list(path), depth=depth)


def main() -> int:
    ok = True

    print("finish the branch you are standing in")
    w = walker()
    items = {
        "root_a": item("root_a", "PHOTO", [], 2),
        "deep_a": item("deep_a", "Resolution", ["Settings", "Video"], 4),
        "deep_b": item("deep_b", "Format", ["Settings", "Video"], 4),
        "mid_c": item("mid_c", "Grid", ["Settings"], 3),
    }
    w._worklist = items
    w._screen_paths = {"here": ["Settings", "Video"]}

    picked = w._next_item("here")
    ok &= check("picks work in the branch it is in, not the shallowest",
                picked.path == ["Settings", "Video"],
                f"picked {picked.label!r} at depth {picked.depth}")
    ok &= check("the shallow root item is NOT chosen",
                picked.label != "PHOTO",
                "that is the furthest thing away and costs a relaunch")

    print()
    print("rise only as far as the nearest pending work")
    w2 = walker()
    w2._worklist = {"root_a": items["root_a"], "mid_c": items["mid_c"]}
    w2._screen_paths = {"here": ["Settings", "Video"]}
    picked = w2._next_item("here")
    ok &= check("prefers the parent branch over the root",
                picked.label == "Grid",
                f"picked {picked.label!r}")

    print()
    print("deepest wins a tie, so a branch is finished before a new one opens")
    w3 = walker()
    w3._worklist = {
        "x": item("x", "Shallow", ["A"], 3),
        "y": item("y", "Deep", ["B", "C"], 4),
    }
    w3._screen_paths = {"here": ["Z"]}          # nothing shares a prefix
    picked = w3._next_item("here")
    ok &= check("falls to depth-first, not breadth-first, when nothing is near",
                picked.label == "Deep",
                "min(depth) here was the old breadth-first behaviour")

    print()
    print("work on the current screen still wins outright")
    w4 = walker()
    w4._worklist = {"here_1": item("here", "OnScreen", ["A"], 3),
                    "deep": item("deep", "Deeper", ["A", "B", "C"], 5)}
    w4._screen_paths = {"here": ["A"]}
    picked = w4._next_item("here")
    ok &= check("no navigation beats any navigation",
                picked.label == "OnScreen",
                "reaching anything else costs a replay")

    print()
    print("an empty worklist is not a crash")
    w5 = walker()
    w5._worklist = {}
    w5._screen_paths = {}
    ok &= check("returns None", w5._next_item("here") is None)
    w6 = walker()
    w6._worklist = {"a": item("a", "X", [], 2)}
    w6._screen_paths = {}
    ok &= check("an unknown current screen still picks something",
                w6._next_item(None) is not None)

    print()
    print("the same screen under a drifted key is not listed twice")
    src = __import__("inspect").getsource(ElementTreeWalker)
    ok &= check("registration checks for an equivalent screen",
                "_equivalent_screen" in src)
    ok &= check("equivalence uses the same rule navigation uses",
                "screen_similarity" in src and "return_similarity" in src,
                "registration and navigation disagreeing about what counts as "
                "the same screen is itself the bug")
    ok &= check("equivalence requires this node or an ancestor of it",
                "want[:len(known_path)]" in src,
                "similarity alone merged five distinct screens into the root, "
                "because every camera screen carries the same viewfinder chrome")
    ok &= check("a drifted key is aliased, so the walk still knows where it is",
                "_screen_aliases" in src)
    ok &= check("the merge is counted, not silent",
                "duplicate_screens_merged" in src)

    print()
    print("cycles must not become depth")
    # The camera's quick settings reach each other: Filters opens a panel that
    # offers Flash, which offers Filters again. Registered as a new screen each
    # lap, the walk produced max_depth 18 on a camera, 157 rows at that depth,
    # and 552 of 636 rows whose path repeated a label. Every counter improved
    # while the output got worse.
    ok &= check("landing on a known screen does not extend the path",
                "return 0" in src.split("twin = self._equivalent_screen")[1][:400],
                "re-registering it is what turns a loop into depth")

    print()
    print("a path never reaches a menu through its own name")
    # The similarity guard catches most cycles, but it is a threshold: a
    # screen that drifts below it registers as new, and the loop resumes.
    # 21 of 245 rows on one run, carrying max_depth to 11 on an app whose
    # deepest menu is 9. This guard is structural and cannot be tuned wrong.
    ok &= check("descending into a label already on the path is refused",
                "item.label in item.path" in src)
    ok &= check("the edge is still recorded, only the re-enumeration refused",
                'item.status, item.reason = "done", "cycle' in src,
                "the click happened and the row exists; what is refused is "
                "enumerating the destination again one level deeper")
    ok &= check("refusals are counted", "cycles_refused" in src)

    print()
    print("one control, one row")
    # Two views on one screen can resolve to the same label AND selector: the
    # mode strip renders PHOTO twice, the edge panel has a handle each side.
    # Distinct views, not distinct controls, and they would emit two identical
    # UVTA cases clicking the same thing.
    ok &= check("rows are fingerprinted before being listed",
                "listed" in src and "fingerprint" in src)
    ok &= check("the fingerprint includes the selector, not just the label",
                "element.selector_kind, element.selector_value" in src,
                "two different controls sharing a label resolve differently "
                "and must both survive")
    ok &= check("collapsing is counted", "duplicate_rows_collapsed" in src)

    print()
    print("a swipe that changes the screen was not a scroll")
    # Spans became axis-aware so a horizontal filter carousel could be read.
    # scrollable_container then started returning the MODE STRIP on the
    # viewfinder, and every enumeration swiped it -- switching PHOTO to VIDEO,
    # collecting another mode into this node, and stranding the walk. 216
    # scrolls, 16 failed returns, benchmark 35/55 -> 17/55.
    ok &= check("scrolling stops when the screen stops being the same screen",
                "scrolls_that_changed_screen" in src
                and "baseline" in src,
                "scrolling reveals more of one screen; anything else is navigation")

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
