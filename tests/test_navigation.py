"""Offline checks for the walker's navigation decision.

`navigation_plan` decides how to get from one known screen to another: how
many times to press BACK, then which labels to click. It is pure arithmetic
over two paths, so it is testable without a device -- which matters, because
three earlier navigation changes on this project were shipped on reasoning
and all three made coverage worse.

    python tests/test_navigation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.element_tree import navigation_plan  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def main() -> int:
    ok = True

    # (here, target, expected, what it is)
    cases = [
        (["A", "B", "C"], ["A", "B", "C", "D"], (0, ["D"]),
         "descend: target is below us"),
        (["A", "B", "C"], ["A", "B"], (1, []),
         "rise: target is an ancestor"),
        (["A", "B", "C"], ["A", "B", "D"], (1, ["D"]),
         "SIBLING: one back, one click -- used to cost a full relaunch"),
        (["A", "B", "C"], ["A", "E", "F"], (2, ["E", "F"]),
         "sibling higher up the tree"),
        (["A", "B", "C"], ["A", "B", "C"], (0, []),
         "already there: no movement at all"),
        ([], ["A"], (0, ["A"]),
         "from the root screen"),
        (["A"], [], (1, []),
         "back to the root screen"),
        (["A", "B", "C"], ["X", "Y"], (3, ["X", "Y"]),
         "unrelated: caller must prefer replay, BACK is unreliable here"),
    ]

    print("plans")
    for here, target, want, what in cases:
        got = navigation_plan(here, target)
        ok &= check(what, got == want, f"{here} -> {target} = {got}")

    print("\nproperties")
    # A plan must always be able to reconstruct the target path.
    for here, target, _, what in cases:
        rises, descents = navigation_plan(here, target)
        rebuilt = list(here[:len(here) - rises]) + descents
        ok &= check(f"plan reconstructs target ({what.split(':')[0]})",
                    rebuilt == list(target), f"{rebuilt} vs {list(target)}")

    # Never press BACK more times than there are screens to rise through.
    for here, target, _, _ in cases:
        rises, _ = navigation_plan(here, target)
        ok &= check("rises never exceed current depth", rises <= len(here),
                    f"{rises} <= {len(here)}")

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
