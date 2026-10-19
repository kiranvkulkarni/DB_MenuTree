"""Samsung Internet screens, checked against REAL dumps (2026-10-19).

The walker holds no browser knowledge; these pin the structural rules the
Internet app forced, so they hold for any app:

  * the Tools menu is fifteen Buttons sharing one id, stacked in a list --
    not a tab strip (a strip is a ROW);
  * the bottom toolbar (Back, Forward, Home, Bookmarks ...) is icon buttons
    named by description, but not a choice: their names share no word;
  * the tab-layout choice states itself only in its description ("Selected,
    Grid, Radio button") -- an option through the profile's option_words, and
    its caption inside it is part of it;
  * a screen with a focused text field is never swept: the keyboard slides up
    after the screen is read, and a swipe across it types.

    python tests/test_sbrowser_screens.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.hierarchy import parse_hierarchy  # noqa: E402
from src.crawler.recursive_walk import RecursiveWalker  # noqa: E402
from src.profile import load_profile  # noqa: E402

APP = "com.sec.android.app.sbrowser"
FIXTURES = Path(__file__).parent / "fixtures"


def check(label, condition, detail=""):
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def dump(name):
    return parse_hierarchy((FIXTURES / name).read_text(encoding="utf-8"))


def main() -> int:
    ok = True
    w = RecursiveWalker(APP, None, {**load_profile(APP), "output_dir": "."})
    w.package = APP

    print("a menu list is not a tab strip")
    tools = dump("sbrowser_tools.xml")
    ok &= check("Tools' items are not taken for tabs",
                w._tab_strip(w._elements(tools), tools) == [])
    camera = dump("pro_screen.xml")
    w.package = "com.sec.android.app.camera"
    ok &= check("(PRO has no mode strip either)", w._tab_strip(w._elements(camera), camera) == [])
    w.package = APP

    print("a toolbar is not a choice")
    main_screen = dump("sbrowser_main.xml")
    roles = {e.label: e.role for e in w._elements(main_screen)}
    for label in ("Bookmarks", "Home", "Forward", "Refresh", "Tools"):
        ok &= check(f"{label} is pressed", roles.get(label) == "title", str(roles.get(label)))

    print("a choice said only in its description is an option (profile)")
    layout = dump("sbrowser_tab_layout.xml")
    roles = {e.label: e.role for e in w._elements(layout)}
    ok &= check("each layout is an option",
                all(roles.get(f"{s}, {n}, Radio button") == "option"
                    for s, n in (("Selected", "Grid"), ("Not selected", "List"),
                                 ("Not selected", "Stack"))), str(roles))
    ok &= check("its caption is part of it", roles.get("Grid") == "option", str(roles.get("Grid")))
    ok &= check("OK and Cancel are still pressed",
                roles.get("OK") == "title" and roles.get("Cancel") == "title")

    print("a screen with the keyboard coming is not swept")

    class Driver:
        def screen_size(self):
            return (1080, 2340)
    w.driver = Driver()
    w._ime = "com.samsung.android.honeyboard"
    search = dump("sbrowser_search_keyboard.xml")
    ok &= check("no swipe on a search screen", w._swipe_span(search) is None)

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
