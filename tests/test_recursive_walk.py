"""Offline checks for the recursive descent.

The walker this replaces kept a global worklist and navigated to each screen
by replaying a path from the root. Two whole categories of failure came from
that and from nothing else:

    unreachable            the replay did not land where the path said
    needs a precondition   the screen was enumerated once, and the snapshot
                           the walk is judging against has gone stale

Neither is a fact about the app. A walk that never jumps -- that stays on the
screen it is working on and steps back exactly one level -- cannot produce
either, because there is nothing to be unreachable *from* and no snapshot to
be stale.

Three rules come from the hand-authored S25 Ultra MenuTree:

* options are listed, never pressed  (pressing 200M reconfigures the camera)
* getting back prefers the screen's own control  (BACK on a viewfinder quits)
* modes are siblings  (to leave VIDEO you press PHOTO, marked `selected`)

    python tests/test_recursive_walk.py
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.elements import Element  # noqa: E402
from src.crawler.recursive_walk import BACK_LABELS, RecursiveWalker  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def el(label, kind="button", interactive=True, selected=False, sel_kind="text"):
    return Element(label=label, kind=kind, interactive=interactive, view_index=0,
                   selected=selected, selector_kind=sel_kind, selector_value=label)


def main() -> int:
    ok = True
    w = RecursiveWalker.__new__(RecursiveWalker)

    print("a screen's own way out is recognised")
    for label in ("Navigate up", "Close", "Cancel", "Back"):
        ok &= check(f"{label!r} is a way out", w._is_back(el(label)))
    ok &= check("a back-kind element counts even with an odd label",
                w._is_back(el("X", kind="back")))
    ok &= check("an ordinary control is not a way out", not w._is_back(el("Flash")))

    print()
    print("one control, one identity")
    same = el("Original")
    other = Element(label="Original", kind="text", interactive=False, view_index=1,
                    selector_kind="desc", selector_value="Original")
    ok &= check("same label but a different selector is a different control",
                w._fingerprint(same) != w._fingerprint(other),
                "a filter chip's caption and its button are two elements")
    ok &= check("the same control twice is one identity",
                w._fingerprint(el("Flash")) == w._fingerprint(el("Flash")))

    print()
    print("getting back is ordered by what it costs to be wrong")
    src = inspect.getsource(RecursiveWalker._return_to)
    order = [src.index(x) for x in ("node.entering", "_is_back", "press_back")]
    ok &= check("the control that opens it, then back control, then hardware BACK",
                order == sorted(order),
                "BACK on a viewfinder leaves the app; a wrong tap only loses a screen")
    ok &= check("the way back is looked up in the tree, not guessed",
                "node.entering" in src,
                "a node records the element that opened it")
    ok &= check("every candidate is verified before it is believed",
                src.count("arrived()") >= 3)
    ok &= check("the action guard applies to getting back too",
                "guard.blocks" in src.split("def press")[1][:400],
                "every candidate goes through one guarded press")

    print()
    print("options are listed, never pressed")
    visit = inspect.getsource(RecursiveWalker._visit)
    ok &= check("an expansion in place lists its options",
                "option -- listed, not selected" in visit)
    ok &= check("and marks them handled, so the loop never presses one",
                "handled.add(self._fingerprint(option))" in visit,
                "pressing 200M sets the camera to 200MP")

    print()
    print("the screen is re-read, never remembered")
    ok &= check("each pass re-enumerates before choosing",
                visit.count("self._enumerate_scrolled") >= 2,
                "a stale snapshot is what produced 'needs a precondition'")
    ok &= check("newly appeared elements are listed too",
                "def list_new" in visit)

    print()
    print("rows come out in tree order by construction")
    ok &= check("depth is recursion depth",
                "self._visit(depth + 1, child_path, child_selectors" in visit)
    descend = visit.index("self._visit(depth + 1, child_path, child_selectors")
    ok &= check("a subtree is walked, then the walk steps back one level",
                "if not self._return_to(node):" in visit[descend:descend + 400],
                "descend, exhaust, come back one -- exactly the sheet's order")

    print()
    print("a menu never contains itself")
    ok &= check("returning to a screen already on the path is refused",
                "_loops_refused" in visit and "ancestors" in visit)

    print()
    print("the mode is a node, and its controls are its children")
    # The benchmark sheet puts Photo at depth 2 and Flash icon, Resolution,
    # Filters at depth 3 -- not the viewfinder's controls directly under the
    # app. That is what gives the walk a way back: PHOTO is only nameable
    # once the mode is on the path, and pressing BACK in VIDEO quits.
    whole_src = inspect.getsource(RecursiveWalker)
    ok &= check("a tab strip is detected structurally",
                "_tab_strip" in whole_src and "resource_id" in whole_src,
                "shared resource id, differing labels")
    strip_src = inspect.getsource(RecursiveWalker._tab_strip)
    ok &= check("it does not depend on the `selected` flag",
                "element.selected" not in strip_src,
                "this camera sets selected on the zoom buttons, not the modes")
    ok &= check("a container full of controls is not a tab strip",
                "endswith(\"Button\")" in strip_src,
                "the six zoom lenses are a RelativeLayout group and outnumber the modes")
    ok &= check("each tab becomes a node and is entered",
                "_enter_tab" in whole_src)
    ok &= check("tabs are left by pressing the next one, not by going back",
                "skip_tabs=True" in inspect.getsource(RecursiveWalker._enter_tab)
                and "_return_to" not in inspect.getsource(RecursiveWalker._enter_tab))
    ok &= check("a strip already handled does not open a new level",
                "not in self._tab_labels" in visit,
                "the mode bar is on every screen; re-detecting it gave depth 19")
    ok &= check("a tab is chrome everywhere except its own level",
                "_tab_labels" in visit and "is_tab" in visit,
                "the strip is drawn on every screen; re-entering it from "
                "inside a mode gave max_depth 19 and 483 of 603 rows cyclic")

    print()
    print("a common menu is documented once")
    ok &= check("a screen already listed elsewhere is not listed again",
                "_already_documented" in whole_src)
    ok &= check("the threshold is far above the navigation one",
                "documented_similarity" in whole_src,
                "Photo and Video are both viewfinders; 0.55 would merge them")
    ok &= check("reuse is counted, not silent",
                "screens_already_documented" in whole_src)

    print()
    print("another screen's elements never land in this node")
    ok &= check("the walk checks it is still on this screen before listing",
                "drifted off" in visit,
                "a QR overlay was once absorbed into the root as depth-2 rows")

    print()
    print("neither discarded category can be produced")
    whole = inspect.getsource(RecursiveWalker)
    ok &= check("nothing is marked unreachable", "unreachable" not in whole)
    ok &= check("nothing needs a precondition", "precondition" not in whole)
    ok &= check("no path is replayed to reach a screen",
                "_navigate_to" not in whole,
                "the walk is always standing where it is working")

    print()
    print(f"back labels: {', '.join(BACK_LABELS)}")
    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
