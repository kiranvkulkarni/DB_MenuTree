#!/usr/bin/env python3
"""Compare a branch walk against a range of the reviewer's To_Be sheet.

Each development step is checked on one branch against the rows the sheet's
author wrote for that branch, so a change can be judged in minutes:

    python tools/compare_tobe.py \\
        --workbook output/<run>/com.sec.android.app.camera_menutree.xlsx \\
        --sheet MenuTree_To_Be --rows 16:192 \\
        --branch output/branch_<stamp>/branch.json

Prints: rows found, rows found at the sheet's depth, rows missing, and tool
rows the sheet does not have -- then every sheet row with its outcome. Labels
are matched fuzzily (the sheet is typed by hand: `besst`, `sspeed`) and each
tool row is used at most once, so a repeated label cannot be counted twice.
"""
import argparse
import json
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.verify.matching import REVIEW, score  # noqa: E402


def sheet_rows(path, sheet, first, last, depth_col=5):
    ws = openpyxl.load_workbook(path)[sheet]
    out = []
    for r in range(first, last + 1):
        for c in range(depth_col, depth_col + 20):
            v = ws.cell(row=r, column=c).value
            if v not in (None, ""):
                out.append((r, c - depth_col + 1, str(v).strip()))
                break
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workbook", required=True)
    p.add_argument("--sheet", default="MenuTree_To_Be")
    p.add_argument("--rows", required=True, help="first:last Excel rows")
    p.add_argument("--branch", required=True, help="branch.json from walk_branch.py")
    p.add_argument("--all", action="store_true", help="print every row, not only problems")
    args = p.parse_args()

    first, last = (int(x) for x in args.rows.split(":"))
    want = sheet_rows(args.workbook, args.sheet, first, last)
    got = json.loads(Path(args.branch).read_text(encoding="utf-8"))["rows"]
    got = [(g["depth"], (g.get("raw_label") or g["label"]).strip(), g.get("note", ""))
           for g in got]

    # ALIGN the two sequences. Both trees are pre-order, but a reviewer may
    # list neighbours in a different order (Camera settings before Navigate
    # up), and labels repeat dozens of times ("switch_widget (On/Off)",
    # "Not available with the current mode..."). Best-match-anywhere paired
    # rows with copies in other branches; a forward cursor was derailed by a
    # single local swap. An LCS-style alignment finds the pairing that keeps
    # the most rows matched across BOTH whole sequences, preferring pairs at
    # the same depth.
    from src.verify.matching import normalise
    wn = [normalise(l) for _, _, l in want]
    gn = [normalise(l) for _, l, _ in got]
    cache = {}

    def sim(i, j):
        key = (wn[i], gn[j])
        if key not in cache:
            if wn[i] == gn[j]:
                cache[key] = 1.0
            elif not wn[i] or not gn[j]:
                cache[key] = 0.0
            else:
                cache[key] = score(want[i][2], got[j][1])[0]
        return cache[key]

    n, m = len(want), len(got)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, below = dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            best = below[j] if below[j] > row[j + 1] else row[j + 1]
            v = sim(i, j)
            if v >= REVIEW:
                gain = 1.0 + (0.5 if want[i][1] == got[j][0] else 0.0) + v * 0.1
                if below[j + 1] + gain > best:
                    best = below[j + 1] + gain
            row[j] = best
    pair = {}
    i = j = 0
    while i < n and j < m:
        v = sim(i, j)
        if v >= REVIEW:
            gain = 1.0 + (0.5 if want[i][1] == got[j][0] else 0.0) + v * 0.1
            if abs(dp[i][j] - (dp[i + 1][j + 1] + gain)) < 1e-9:
                pair[i] = j
                i += 1
                j += 1
                continue
        if dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1

    used, lines, misses = set(pair.values()), [], []
    found = at_depth = 0
    for i, (r, d, label) in enumerate(want):
        if i in pair:
            found += 1
            gd = got[pair[i]][0]
            at_depth += gd == d
            mark = "ok " if gd == d else f"d{gd}"
            lines.append((gd != d, f"  row {r:<4} d{d} {mark:<4} {label[:56]}"))
        else:
            misses.append((r, d, label))
            lines.append((True, f"  row {r:<4} d{d} MISS {label[:56]}"))
    extra = [g for i, g in enumerate(got) if i not in used]

    print(f"sheet rows {first}-{last}: {len(want)}    tool rows: {len(got)}")
    print(f"  found            {found}/{len(want)} ({100 * found // max(1, len(want))}%)")
    print(f"  at sheet depth   {at_depth}/{found}")
    print(f"  missing          {len(misses)}")
    print(f"  extra            {len(extra)}")
    print("\nmissing:")
    for r, d, label in misses:
        print(f"  row {r:<4} d{d}  {label[:64]}")
    print("\nextra (tool rows the sheet does not have):")
    for d, label, note in extra[:60]:
        print(f"  d{d}  {label[:46]:<48} {note[:36]}")
    print("\nrows" + (" (problems only)" if not args.all else "") + ":")
    for problem, line in lines:
        if args.all or problem:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
