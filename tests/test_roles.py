"""Element roles, checked against a REAL screen dump.

The roles in docs/HANDLING.md decide what the walker does with every element.
They are read from structure alone -- class, clickable, checkable, which
clickable row a label sits in -- so the walker stays app-agnostic. This test
pins them on the camera's Settings screen, captured from the device, because
that one screen holds every case that went wrong:

  * `Shot suggestions` carries a Switch that is NOT clickable: pressing the
    row flips the setting. The walker once took it for a menu.
  * `Watermark` carries a Switch that IS clickable, behind a divider: the row
    opens a screen and the switch is a separate target.
  * `Prioritize quality`, `HEVC (high efficiency)` are summary lines inside a
    row. Pressing one opened the row's screen and filed it under the summary.
  * The launcher's Edge panels handle is drawn over the app from another
    package, and was listed as if it were a camera menu.

    python tests/test_roles.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.elements import enumerate_elements  # noqa: E402
from src.crawler.hierarchy import parse_hierarchy  # noqa: E402
from src.crawler.recursive_walk import RecursiveWalker  # noqa: E402

APP = "com.sec.android.app.camera"
FIXTURE = Path(__file__).parent / "fixtures" / "settings_list.xml"


def check(label, condition, detail=""):
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def main() -> int:
    ok = True
    views = parse_hierarchy(FIXTURE.read_text(encoding="utf-8"))
    elements = enumerate_elements(views, APP)
    w = RecursiveWalker(APP, None, {"output_dir": "."})
    roles = {}
    for e in elements:
        roles.setdefault(e.label, []).append(w._row_role(e, views)[0])

    print("rows are told apart by structure")
    ok &= check("Shot suggestions is a switch row",
                roles.get("Shot suggestions") == ["switch_row"],
                str(roles.get("Shot suggestions")))
    ok &= check("its switch is operated by the row, not on its own",
                "inline" in roles.get("switch_widget", []))
    ok &= check("Watermark's title opens its screen, its switch is separate",
                sorted(roles.get("Watermark", [])) == ["switch", "title"],
                str(roles.get("Watermark")))
    for title in ("Scanning", "Photo enhancer", "Custom filters", "Video format"):
        ok &= check(f"{title} is a title", roles.get(title) == ["title"],
                    str(roles.get(title)))

    print()
    print("summary lines are never pressed")
    for summary in ("Prioritize quality", "HEVC (high efficiency)",
                    "Use for 30 fps videos only"):
        ok &= check(f"{summary!r} is a summary", roles.get(summary) == ["summary"],
                    str(roles.get(summary)))

    print()
    print("another package's overlay is not the app's")
    labels = [e.label for e in elements]
    ok &= check("the launcher's Edge panels handle is excluded",
                "Edge panels" not in labels, "package com.sec.android.app.launcher")
    ok &= check("the status bar is excluded", not any(":" in l and len(l) <= 5 for l in labels))
    ok &= check("the app's own rows remain", "Watermark" in labels and "Scanning" in labels)

    print()
    print("a radio row is an option, and is never selected")
    # Pressing such a row's title SELECTS the option: the walk once switched
    # Photo enhancer to `Prioritize speed` and Auto FPS to `Off` this way.
    pe = parse_hierarchy((Path(__file__).parent / "fixtures" /
                          "photo_enhancer.xml").read_text(encoding="utf-8"))
    pe_roles = {}
    for e in enumerate_elements(pe, APP):
        pe_roles.setdefault(e.label, []).append(w._row_role(e, pe)[0])
    for option in ("Prioritize quality", "Balanced", "Prioritize speed"):
        ok &= check(f"{option!r} is an option", pe_roles.get(option) == ["option"],
                    str(pe_roles.get(option)))
    ok &= check("the radio buttons themselves are options, not switches",
                set(pe_roles.get("radio", [])) == {"option"},
                str(pe_roles.get("radio")))
    ok &= check("Scene detection is a switch row",
                pe_roles.get("Scene detection") == ["switch_row"])

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
