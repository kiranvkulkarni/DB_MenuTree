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

    # A switch NAMED like its item (found by its description) comes after
    # the item's whole subtree; it still goes beside the item (L54-L56).
    def at(label, depth, path, kind="text", note=""):
        r = row(label, depth)
        r.kind, r.path, r.note = kind, path, note
        return r
    here, sub = ["Settings"], ["Settings", "Log"]
    named = at("Log (On/Off)", 6, here, kind="toggle")
    named.raw_label, named.widget_id = "Log", "switch_widget"
    rows = [at("Log", 6, here),
            at("Pro video mode only", 6, here, note="summary -- listed, not pressed"),
            at("Log [Title]", 7, sub),
            at("Off", 7, sub, kind="option", note="switch state"),
            at("Video and Pro video modes", 8, sub + ["On"]),
            named,
            at("Off", 7, ["Settings", "Watermark"]),
            at("Off (On/Off)", 7, ["Settings", "Watermark"], kind="toggle"),
            at("Default (On/Off)", 8, sub, kind="toggle", note="option -- listed, not selected")]
    rows[-2].raw_label = "Off"
    merged = w._merge_widgets(rows)
    out = {(r.label, tuple(r.path)): r.widget for r in merged}
    ok &= check("a named switch goes beside the last line of its item",
                out.get(("Pro video mode only", tuple(here))) == "switch_widget (On/Off)"
                and ("Log (On/Off)", tuple(here)) not in out)
    ok &= check("without an id it is called a switch",
                out.get(("Off", ("Settings", "Watermark"))) == "switch (On/Off)")
    ok &= check("an option is never taken for a switch",
                ("Default (On/Off)", tuple(sub)) in out)

    # Re-exporting an existing walk applies the same rules to its rows.
    again = [TreeNode.from_dict(r.to_dict()) for r in rows[:6]]
    ok &= check("a row read back from the rows file is the same row",
                [r.to_dict() for r in again] == [r.to_dict() for r in rows[:6]])
    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
