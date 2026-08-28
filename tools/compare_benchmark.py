#!/usr/bin/env python3
"""Compare a discovery run against the hand-authored benchmark subtree.

A coverage percentage cannot say whether the tree has the right *shape*. The
benchmark is one mode transcribed from the sheet a manual engineer wrote, and
the only useful question is: for each row they listed, did the crawler find
it, and at the depth they put it?

Rows marked `(manual)` in the benchmark are gestures and physical keys --
"Zoom in/out by finger", "Long press the shutter button". They cannot appear
in an XML dump, so they are reported separately rather than counted as
misses; a tool that claimed to find them would be lying.

    python tools/compare_benchmark.py --rows output/<run>/menutree_rows.json \
        --benchmark tests/benchmark/photo_mode.txt --mode PHOTO
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.verify.matching import REVIEW, load_aliases, normalise, score  # noqa: E402


def read_benchmark(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        depth, _, rest = line.strip().partition(" ")
        manual = "(manual)" in rest
        label = re.sub(r"\(manual\)", "", rest).strip()
        rows.append({"depth": int(depth), "label": label, "manual": manual})
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rows", required=True)
    p.add_argument("--benchmark", required=True)
    p.add_argument("--mode", default="PHOTO",
                   help="the depth-2 node to compare under")
    p.add_argument("--aliases", default="",
                   help="sheet wording -> what the dump exposes. Without it "
                        "`12MP` reads as missing when the control is present "
                        "and simply called BACK_CAMERA_PICTURE_SIZE_NORMAL.")
    args = p.parse_args()

    wanted = read_benchmark(args.benchmark)
    found = json.loads(Path(args.rows).read_text(encoding="utf-8"))["rows"]

    # Everything under the named mode, plus the mode row itself.
    mode = args.mode.strip().lower()
    subtree = [r for r in found
               if (r["label"].strip().lower() == mode and r["depth"] == 2)
               or (r.get("path") and r["path"][0].strip().lower() == mode)]

    aliases = load_aliases(args.aliases) if args.aliases else {}
    hits, misses, manual = [], [], []
    for row in wanted:
        if row["manual"]:
            manual.append(row)
            continue
        targets = {normalise(t) for t in aliases.get(row["label"], [])}
        if targets:
            alias_hit = next(
                (c for c in subtree
                 if normalise(c.get("raw_label") or c["label"]) in targets), None)
            if alias_hit is not None:
                hits.append((row, alias_hit, 1.0))
                continue
        best, best_score = None, 0.0
        for candidate in subtree:
            value, _ = score(row["label"], candidate.get("raw_label") or candidate["label"])
            if value > best_score:
                best, best_score = candidate, value
        if best is not None and best_score >= REVIEW:
            hits.append((row, best, best_score))
        else:
            misses.append((row, best, best_score))

    checkable = len(wanted) - len(manual)
    print(f"benchmark : {args.benchmark}")
    print(f"run       : {args.rows}")
    print(f"subtree   : {len(subtree)} rows under {args.mode!r}")
    print()
    print(f"  {len(hits)}/{checkable} benchmark rows found "
          f"({100.0 * len(hits) / max(1, checkable):.0f}%)")
    print(f"  {len(manual)} rows are gestures or hardware keys, not in any dump")
    print()

    depth_ok = sum(1 for row, got, _ in hits if got["depth"] == row["depth"])
    print(f"  of those found, {depth_ok}/{len(hits)} sit at the benchmark's depth")
    off = [(r, g) for r, g, _ in hits if g["depth"] != r["depth"]]
    for row, got in off[:8]:
        print(f"      {row['label'][:34]:<36} sheet d{row['depth']}  run d{got['depth']}")

    if misses:
        print()
        print("  not found:")
        for row, best, value in misses:
            near = f"closest {best['label'][:26]!r} at {value:.2f}" if best else "nothing close"
            print(f"      d{row['depth']} {row['label'][:40]:<42} {near}")

    if manual:
        print()
        print("  manual-only, correctly absent:")
        for row in manual:
            print(f"      d{row['depth']} {row['label']}")

    return 0 if not misses else 1


if __name__ == "__main__":
    sys.exit(main())
