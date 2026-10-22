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
    # PRO's bar of dials, as dumped on the phone (2026-10-16). Over the ISO
    # dial the bar still shows, but its readings move with the light.
    from types import SimpleNamespace
    from src.crawler.elements import Element

    def view(parent, bounds, rid="", clickable=False, cls="LinearLayout", scrollable=False):
        return {"parent": parent, "bounds": bounds, "resource_id": rid,
                "clickable": clickable, "class": cls, "scrollable": scrollable}
    views = [view(None, "[0,0][1080,2340]", cls="FrameLayout"),
             view(0, "[122,1481][959,1583]", "collapsed_panel", cls="RecyclerView", scrollable=True),
             view(1, "[143,1496][330,1568]", "pro_collapsed_item", clickable=True),   # ISO
             view(2, "[219,1511][281,1556]", "collapsed_item_value", cls="TextView"),  # 800
             view(1, "[336,1496][490,1568]", "pro_collapsed_item", clickable=True),   # SPEED
             view(4, "[354,1511][441,1556]", "collapsed_item_value", cls="TextView"),  # 1/30s
             view(1, "[709,1496][938,1568]", "pro_collapsed_item", clickable=True),   # WB
             view(0, "[525,81][620,176]", "quick_setting_button_main", clickable=True),  # Flash
             view(0, "[60,1661][204,1805]", "left_button", clickable=True),          # Auto ISO
             view(0, "[508,1668][573,1725]", "dialer_level", cls="TextView"),          # 800
             view(None, "[0,300][1080,2000]", cls="RecyclerView", scrollable=True),
             view(10, "[0,1200][1080,1350]", "row", clickable=True),                  # Reset settings
             ]

    def el(label, index, desc="", clickable=False):
        e = Element(label=label, kind="button" if clickable else "text",
                    interactive=clickable, view_index=index,
                    selector_kind="desc" if desc else "text", selector_value=desc or label)
        e.slot, e.desc = (views[index]["resource_id"], views[index]["bounds"]), desc
        return e
    pro = [el("ISO", 2, "ISO", True), el("800", 3), el("SPEED", 4, "SPEED", True),
           el("1/30s", 5), el("WB", 6, "WB", True), el("Flash", 7, "Flash", True)]
    over_iso = [el("ISO", 2, "ISO", True), el("3200", 3), el("SPEED", 4, "SPEED", True),
                el("1/90s", 5), el("WB", 6, "WB", True),
                el("BACK_FLASH_OFF", 7, "Flash", True),
                el("Auto ISO", 8, "Auto ISO", True), el("800", 9)]
    behind = {w._fingerprint(e) for e in over_iso[:5] if e.label in ("ISO", "SPEED", "WB")}
    still = w._still_behind(over_iso, views, SimpleNamespace(elements=pro), behind)
    labels = {e.label for e in over_iso if w._fingerprint(e) in still}
    ok &= check("a dial's reading that moved with the light is not a new row",
                {"3200", "1/90s"} <= labels)
    ok &= check("a chip whose text changed but whose id, place and description did not, is the same chip",
                "BACK_FLASH_OFF" in labels)
    ok &= check("what the dial panel adds is kept (Auto ISO, the dial's level)",
                not {"Auto ISO", "800"} & labels)
    ok &= check("nothing is taken for the screen behind when it is no longer showing",
                not w._still_behind(over_iso, views, SimpleNamespace(elements=pro), set()))
    ok &= check("a chip in a strip is not a row of a list (PRO's Reset)",
                not w._fills_list(views, 2))
    ok &= check("a settings row fills its list (Reset settings)", w._fills_list(views, 11))

    print("suggestions by resource id: a preview button, not a settings row of the same name")
    from src.crawler.elements import Element
    sw = RecursiveWalker("x", None, {"output_dir": ".", "suggestions": ["text_scan_button"]})
    button = Element(label="Scan text", kind="button", interactive=True, view_index=0)
    button.slot = ("com.example:id/text_scan_button", "")
    row_ = Element(label="Scan text", kind="title", interactive=True, view_index=0)
    row_.slot = ("android:id/title", "")
    ok &= check("the preview button is a suggestion",
                "suggestion" in (sw._worth_pressing(button) or ""))
    ok &= check("the settings row is pressed", "suggestion" not in (sw._worth_pressing(row_) or ""))

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
