"""Rules agreed in the first review of the MenuTree (2026-10-15).

A radio or checkbox is written beside its option, a switch beside the last
line of its row -- on the same row, in the next column.

    python tests/test_review_rules.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.element_tree import TreeNode  # noqa: E402
from src.crawler.recursive_walk import RecursiveWalker  # noqa: E402


def row(label, depth=6, ctx="", widget=False, raw=None):
    r = TreeNode(label=label, kind="text", depth=depth, raw_label=raw or label)
    r.context, r.unlabelled_widget = ctx, widget
    return r


def check(name, condition):
    print(f"  [{'ok' if condition else 'FAIL'}] {name}")
    return condition


def main() -> int:
    w = RecursiveWalker("x", None, {"output_dir": "."})
    rows = [row("radio (On/Off)", ctx="Balanced", widget=True, raw="radio"),
            row("Balanced"),
            row("Speed up capture time by reducing optimization.", ctx="Balanced"),
            row("Scene detection"),
            row("Automatically adjust the color and contrast", ctx="Scene detection"),
            row("switch_widget (On/Off)", ctx="Scene detection", widget=True, raw="switch_widget"),
            row("Scan text", 5),
            row("switch_widget (On/Off)", 5, ctx="Scan text", widget=True, raw="switch_widget")]
    out = {r.label: r.widget for r in w._merge_widgets(rows)}
    ok = True
    ok &= check("a radio goes beside its option", out.get("Balanced") == "radio (On/Off)")
    ok &= check("a switch goes beside the last line of its row",
                out.get("Automatically adjust the color and contrast") == "switch_widget (On/Off)")
    ok &= check("a switch with no description goes beside its title",
                out.get("Scan text") == "switch_widget (On/Off)")
    ok &= check("no widget is left on a row of its own",
                "radio (On/Off)" not in out and "switch_widget (On/Off)" not in out)
    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
