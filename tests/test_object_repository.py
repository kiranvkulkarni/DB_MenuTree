"""The object repository and run file names (agreed 2026-10-20).

    python tests/test_object_repository.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.generator import object_repository as repo  # noqa: E402
from src.run_files import find, run_file  # noqa: E402


def check(label, condition, detail=""):
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def row(label, path, by="text", value=None, rid="", long=False, xpath="/a[1]"):
    return {
        "label": label + (" (long press)" if long else ""), "raw_label": label,
        "path": path, "kind": "button", "selector_kind": by,
        "selector_value": value or label,
        "locators": {"text": label if by == "text" else "", "content_desc": label if by == "desc" else "",
                     "resource_id": rid, "class": "android.widget.Button",
                     "package": "com.example", "xpath": xpath},
    }


def main() -> int:
    ok = True
    rows = [
        row("Flash", ["PORTRAIT", "Quick controls"], by="desc",
            rid="com.example:id/chip"),
        row("Flash", ["PORTRAIT", "Quick controls"], by="desc",
            rid="com.example:id/chip"),                       # the same element again
        row("Flash", ["PORTRAIT"], by="desc", rid="com.example:id/top", xpath="/b[1]"),
        row("Bookmarks", [], long=True, xpath="/c[1]"),
        {"label": "Launch 1", "raw_label": "Launch 1", "path": [], "kind": "text",
         "locators": {}},
    ]
    entries, names = repo.build(rows)
    print("names are stable and per screen")
    ok &= check("screen + label", names[0] == "PORTRAIT.QuickControls.Flash", names[0])
    ok &= check("the same element twice is one entry", names[1] == names[0] and len(entries) == 3)
    ok &= check("the same label on another screen is another entry", names[2] == "PORTRAIT.Flash")
    ok &= check("a long press is its own entry", names[3] == "Main.BookmarksLongPress", names[3])
    ok &= check("a row with no UI element has none", names[4] == "")
    e = entries["PORTRAIT.QuickControls.Flash"]
    ok &= check("all locators kept, preferred marked",
                e["preferred"] == {"by": "desc", "value": "Flash"}
                and e["locators"]["resource_id"] == "com.example:id/chip"
                and e["locators"]["uiautomator"] == 'new UiSelector().description("Flash")')

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "com.example_20261020_101010_001"
        folder.mkdir()
        print("files are named after their run")
        ok &= check("unique name", run_file(folder, "xlsx").name
                    == "com.example_20261020_101010_001_menutree.xlsx")
        (folder / "menutree_rows.json").write_text("{}", encoding="utf-8")
        ok &= check("an older run is still found", find(folder, "rows").name == "menutree_rows.json")
        pages = repo.write_page_objects(entries, run_file(folder, "pages"), "com.example")
        spec = importlib.util.spec_from_file_location("pages", pages)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print("page objects import and hold the locators")
        ok &= check("one class per screen",
                    hasattr(module, "PORTRAIT_QuickControls") and hasattr(module, "Main"))
        ok &= check("locator by attribute",
                    module.PORTRAIT_QuickControls.Flash.best == ("desc", "Flash"))
        ok &= check("long press marked", module.Main.BookmarksLongPress.long_press)

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
