"""Build a workbook shaped like the hand-authored MenuTree specification.

Mirrors the real deliverable's layout so the reader can be tested without
the actual sheet: a summary block at the top, a header row carrying
`Test Result / Defect ID / Comments` followed by `1 Depth ... N Depth`, and
one row per element with its label in the column matching its depth.

The labels are generic and the shapes are the real sheet's (Modes and
Settings sheets), including the awkward cases: [bracketed] context rows,
`[Title]` / `(On/Off)` annotations, and a permission dialog whose branches sit
under it. Company sheets themselves are never committed.

    python tests/make_spec_fixture.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path("tests/spec_fixture.xlsx")

# (depth, label) in the sheet's own top-to-bottom tree order. The labels are
# generic; the SHAPES are the real sheet's: bracketed context rows, `[Title]`
# and `(On/Off)` annotations, radio rows, a permission dialog with its
# branches, double spaces and typos as hand-typed sheets have them, depth 7.
MODES = [
    (1, "App"),
    (2, "[First launch]"),
    (2, "Feature tags [ON/OFF] on 3rd launch"),
    (3, "[When the permission is OFF on the device]"),
    (3, "Turn on feature tags[Title]"),
    (3, "Learn more"),
    (4, "Feature tags [Title]"),
    (4, "Feature tags ...explained."),
    (4, "OK"),
    (3, "Cancel"),
    (3, "Turn on"),
    (4, "To continue, turn on the device setting"),
    (4, "No Thanks"),
    (4, "OK"),
    (3, "Precise"),
    (3, "Approximate"),
    (4, "while using this app"),
    (4, "Only this time"),
    (4, "Don't Allow"),
    (2, "[Main view]"),
    (2, "Mode A"),
    (3, "Mode A [Title]"),
    (3, "Light icon"),
    (4, "On"),
    (4, "Off"),
    (4, "Auto"),
    (3, "Size"),
    (4, "Small"),
    (4, "Medium"),
    (4, "Large"),
    (4, "Extra large"),
    (3, "Live Preview"),
    (4, "ON"),
    (4, "OFF"),
    (3, "Styles"),
    (4, "Original"),
    (4, "Style one"),
    (4, "Style two"),
    (4, "Style three"),
]

SETTINGS = [
    (1, "App"),
    (2, "Quick Panel"),
    (3, "Settings icon"),
    (4, "Back Icon"),
    (4, "App Settings [Title]"),
    (4, "Smart features [subtitle]"),
    (4, "Scanning"),
    (5, "Scan items and text (On/Off)"),
    (5, "Scan Text(on/off)"),
    (5, "Scan codes(on/off)"),
    (5, "Back key icon"),
    (4, "Suggestion"),
    (5, "ON"),
    (5, "OFF"),
    (4, "Enhance"),
    (5, "Prefer qualty (Radio button On/Off)"),
    (5, "Balance (Radio button On/Off)"),
    (5, "Prefer sped (Radio button On/Off)"),
    (5, "Auto adjust (On/Off)"),
    (5, "Back key icon"),
    (4, "Files (sub-title)"),
    (4, "File Format"),
    (5, "Compact files(ON/OFF)"),
    (5, "Advanced file fromat"),
    (6, "Format one"),
    (6, "Format one and two"),
    (6, "Format two"),
    (6, "Back key  icon"),
    (4, "Label(On/Off)"),
    (5, "ON"),
    (5, "select information that the label includes"),
    (6, "Device name"),
    (7, "edit"),
    (6, "Date"),
    (6, "Time"),
]


def build_sheet(book, title: str, entries, max_depth: int, first: bool):
    sheet = book.active if first else book.create_sheet()
    sheet.title = title

    # Summary block, as in the real workbook.
    sheet.cell(row=2, column=2, value="PRELOAD")
    sheet.cell(row=2, column=5, value="Total")
    sheet.cell(row=2, column=7, value="Pass")
    sheet.cell(row=2, column=9, value="Fail")
    sheet.cell(row=2, column=11, value="NA")
    sheet.cell(row=2, column=13, value="NT")
    sheet.cell(row=3, column=2, value="PASS")
    sheet.cell(row=3, column=5, value=len(entries))

    header_row = 5
    sheet.cell(row=header_row, column=1, value="Test Result")
    sheet.cell(row=header_row, column=2, value="Defect ID")
    sheet.cell(row=header_row, column=3, value="Comments")
    for depth in range(1, max_depth + 1):
        sheet.cell(row=header_row, column=3 + depth, value=f"{depth} Depth")

    for offset, (depth, label) in enumerate(entries):
        row = header_row + 1 + offset
        sheet.cell(row=row, column=1, value="Pass")
        sheet.cell(row=row, column=3 + depth, value=label)
    return sheet


def main() -> int:
    from openpyxl import Workbook

    book = Workbook()
    build_sheet(book, "Modes", MODES, 18, first=True)
    build_sheet(book, "Settings", SETTINGS, 7, first=False)

    # A sheet with no depth columns, to prove it is skipped rather than
    # misread -- the real workbook has a Summary tab like this.
    summary = book.create_sheet()
    summary.title = "Summary"
    summary.cell(row=2, column=2, value="PROJECT INFORMATION")
    summary.cell(row=4, column=2, value="Test result")
    summary.cell(row=5, column=2, value="Pass")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    book.save(OUT)
    print(f"wrote {OUT} ({len(MODES)} Modes + {len(SETTINGS)} Settings rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
