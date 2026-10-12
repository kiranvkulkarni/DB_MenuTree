"""The scroll sweep keeps a screen's rows in the order they are drawn.

A row cut off at the bottom of one screenful shows its remaining lines on
the next. Appending put them at the end, so `Save selfies as previewed` came
out title / switch / summary where the sheet has title / summary / switch.

    python tests/test_sweep_order.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.crawler.element_tree as et  # noqa: E402
from src.crawler.element_tree import ElementTreeWalker  # noqa: E402
from src.crawler.elements import Element  # noqa: E402


class Screens(ElementTreeWalker):
    """A walker whose 'device' is a list of screenfuls, one per scroll."""

    def __init__(self, screens):
        self.screens, self.i = screens, 0
        self.driver, self.max_scrolls, self._scroll_left_screen = object(), 10, 0

    def _elements(self, views):
        return [Element(label=l, kind="text", interactive=False, view_index=0)
                for l in views[0]["labels"]]

    def _fingerprint(self, element):
        return element.label

    def _swipe_span(self, views):
        return ("v", 0, 0, 1)

    def _await_stable(self):
        return self.i, self.screens[self.i], "pkg"

    def _scroll_once(self, span, down=True):
        if self.i + 1 < len(self.screens):
            self.i += 1
        return self.screens[self.i]

    def _same_container(self, container_id, current):
        return True


def sweep(*screens):
    screens = [[{"labels": list(s)}] for s in screens]
    return [e.label for e in Screens(screens)._enumerate_scrolled(screens[0])]


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + ("" if ok else f"\n        got  {got}\n        want {want}"))
    return ok


def main() -> int:
    et.scrollable_container = lambda views: None
    ok = True
    ok &= check("a row's summary from the next screenful stays inside the row",
                sweep(["Nav", "A", "B title", "B switch"],
                      ["B title", "B summary", "B switch", "C"]),
                ["Nav", "A", "B title", "B summary", "B switch", "C"])
    ok &= check("new rows above the first known one go just above it",
                sweep(["Nav", "A", "C"], ["B", "C", "D"]),
                ["Nav", "A", "B", "C", "D"])
    ok &= check("a screenful with nothing known is appended",
                sweep(["Nav", "A"], ["X", "Y"]),
                ["Nav", "A", "X", "Y"])
    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
