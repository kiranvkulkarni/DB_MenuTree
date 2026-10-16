"""PRO's dials, checked against REAL screen dumps (2026-10-16).

PRO shows a bar of dials -- ISO 2500, SPEED 1/15s, EV, FOCUS MF, WB 2300K --
and pressing one opens a panel with an Auto button, the dial and Close.
Three things went wrong there, all visible on these two screens:

  * The dial is a scrollable SeekBar. Every sweep for rows below the fold
    swiped it and turned the dial: ISO went manual, SPEED to 30 s, and the
    bar grew a "Reset" chip that resets every dial.
  * SPEED, EV and FOCUS name themselves by description and hold their value
    as text; the value was taken for the dial's title and pressed.
  * The bar's "Reset" chip sits in a list that scrolls sideways, and was
    opened as though it were Settings' "Reset settings" row.

    python tests/test_pro_dials.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.hierarchy import parse_hierarchy, scrollable_container  # noqa: E402
from src.crawler.recursive_walk import RecursiveWalker  # noqa: E402

APP = "com.sec.android.app.camera"
FIXTURES = Path(__file__).parent / "fixtures"


def check(label, condition, detail=""):
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def main() -> int:
    ok = True
    w = RecursiveWalker(APP, None, {"output_dir": "."})
    w.package = APP

    panel = parse_hierarchy((FIXTURES / "pro_iso_panel.xml").read_text(encoding="utf-8"))
    print("a dial is never swiped")
    ok &= check("the ISO panel's only scrollable is its dial",
                [v.get("class", "").split(".")[-1] for v in panel if v.get("scrollable")]
                == ["SeekBar"])
    ok &= check("so there is nothing to scroll there", scrollable_container(panel) is None)
    ok &= check("it can still be read when asked for (a mode strip)",
                scrollable_container(panel, sliders=True) is not None)

    print("a label on a dial is never pressed")
    on_panel = {e.label + str(e.view_index): e for e in w._elements(panel)}
    level = next((e for e in on_panel.values()
                  if (panel[e.view_index].get("resource_id") or "").endswith("dialer_level")),
                 None)
    ok &= check("the dial's level is on the slider",
                level is not None and level.on_slider, str(level and level.label))
    ok &= check("so it is listed, not pressed (a tap there turns the dial)",
                level is not None and (w._worth_pressing(level) or "").startswith("on a slider"),
                str(level and w._worth_pressing(level)))
    ok &= check("the Auto button beside it is not",
                not any(e.on_slider for e in on_panel.values() if e.label.startswith("Auto")))

    pro = parse_hierarchy((FIXTURES / "pro_screen.xml").read_text(encoding="utf-8"))
    ok &= check("the mode strip's own tabs are not taken for dial labels",
                not any(e.on_slider for e in w._elements(pro)))
    elements = w._elements(pro)
    roles = {e.label: e.role for e in elements}
    print("a dial's value is not its title")
    ok &= check("SPEED is pressed", roles.get("SPEED") == "title", str(roles.get("SPEED")))
    ok &= check("its value 1/15s is listed, not pressed",
                roles.get("1/15s") == "summary", str(roles.get("1/15s")))
    ok &= check("FOCUS's value MF too", roles.get("MF") == "summary", str(roles.get("MF")))
    ok &= check("ISO's value 2500 as before", roles.get("2500") == "summary",
                str(roles.get("2500")))

    print("the bar's Reset is not a row of a list")
    reset = next((e for e in elements if e.label == "Reset"), None)
    ok &= check("Reset is on the bar", reset is not None)
    ok &= check("it does not fill its list", reset is not None and not reset.full_row)
    w.open_guarded_rows = True
    ok &= check("so the guard refuses it even where guarded rows are opened",
                reset is not None and (w._worth_pressing(reset) or "").startswith("action guard"),
                str(reset and w._worth_pressing(reset)))

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
