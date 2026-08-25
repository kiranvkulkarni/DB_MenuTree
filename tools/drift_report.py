"""Turn a verification run into a list of what drifted, grouped by branch.

    python tools/drift_report.py                     # newest run
    python tools/drift_report.py output/verify_...   # a specific run

A verification run answers "did this build match the sheet?". When the sheet
is older than the build, the answer is a few hundred Fails, and a flat list
of them is not something anyone can act on. This groups them by the branch
they live in and classifies each by how close the nearest control on screen
was, which is what separates the three cases that need different responses:

    renamed        the control is there under another name  -> add an alias,
                   or update the sheet's wording
    restructured   the branch moved or gained a level       -> re-author that
                   part of the sheet
    absent         nothing close is on screen               -> either a real
                   regression, or a feature the build dropped

The classification is a starting point for triage, not a verdict. Only
someone who knows the product can say whether an absent control is a dropped
feature or a defect.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLOSEST = re.compile(r"closest(?: was)? '([^']+)' at ([0-9.]+)")
STEP = re.compile(r"path step not found on screen: '([^']+)'")

# How near the best on-screen candidate was.
RENAMED_FROM = 0.30      # at or above this: something similar is there
PRESENT_FROM = 0.60      # at or above this it would have matched


def classify(score: float) -> str:
    if score >= RENAMED_FROM:
        return "renamed?"
    return "absent"


def load(path: Path) -> dict:
    for name in ("verify_results.json", "verify_results.partial.json"):
        candidate = path / name if path.is_dir() else path
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            data["_source"] = candidate
            return data
    raise SystemExit(f"no verify results under {path}")


def main() -> int:
    p = argparse.ArgumentParser(description="Summarise spec-vs-build drift")
    p.add_argument("run", nargs="?", help="run folder (default: newest)")
    p.add_argument("--output-root", default="./output")
    p.add_argument("--top", type=int, default=25, help="branches to list")
    args = p.parse_args()

    if args.run:
        folder = Path(args.run)
    else:
        runs = sorted(Path(args.output_root).glob("verify_*"),
                      key=lambda f: f.stat().st_mtime)
        if not runs:
            raise SystemExit("no verify runs found")
        folder = runs[-1]

    data = load(folder)
    results = data["results"]
    stats = data.get("stats", {})
    partial = data.get("partial", False)

    counts = defaultdict(int)
    for r in results:
        counts[r["result"]] += 1
    judged = counts["Pass"] + counts["Fail"]

    print()
    print("=" * 74)
    print(f"  DRIFT REPORT  {'(PARTIAL RUN)' if partial else ''}")
    print("=" * 74)
    print(f"  run          : {folder.name}")
    print(f"  rows checked : {len(results)} of {stats.get('rows_in_spec', '?')}")
    print(f"  Pass {counts['Pass']}   Fail {counts['Fail']}   NA {counts['NA']}   "
          f"NT {counts['NT']}")
    if judged:
        print(f"  pass rate    : {100.0 * counts['Pass'] / judged:.1f}%  "
              f"over {judged} judged row(s)")
        if judged < 0.6 * len(results):
            print("  WARNING: most rows were NA -- this rate describes a slice, "
                  "not the build.")
    print("=" * 74)

    # Group failures by the branch they sit in: the first two path steps.
    branches = defaultdict(list)
    for r in results:
        if r["result"] != "Fail":
            continue
        branch = " > ".join(r["path"][:2]) or "<top level>"
        branches[branch].append(r)

    print()
    print(f"  {len(branches)} branch(es) contain failures, worst first:")
    print()
    ordered = sorted(branches.items(), key=lambda kv: -len(kv[1]))
    for branch, rows in ordered[:args.top]:
        best = 0.0
        kinds = defaultdict(int)
        for r in rows:
            m = CLOSEST.search(r["detail"])
            score = float(m.group(2)) if m else 0.0
            best = max(best, score)
            kinds[classify(score)] += 1
        verdict = "renamed?" if kinds["renamed?"] >= kinds["absent"] else "absent"
        print(f"  {len(rows):>4} row(s)  [{verdict:<9}] {branch[:52]}")
        # Name a couple of concrete examples: what was wanted, what was there.
        shown = 0
        for r in rows:
            m = CLOSEST.search(r["detail"])
            if not m:
                continue
            print(f"          {r['label'][:34]!r:<36} -> nearest "
                  f"{m.group(1)[:26]!r:<28} {m.group(2)}")
            shown += 1
            if shown >= 2:
                break

    if len(ordered) > args.top:
        print(f"\n  ... and {len(ordered) - args.top} more branch(es)")

    # The single most useful list: labels the sheet expects that the build
    # has no near match for anywhere.
    absent = {}
    for r in results:
        if r["result"] != "Fail":
            continue
        m = CLOSEST.search(r["detail"])
        score = float(m.group(2)) if m else 0.0
        if score < RENAMED_FROM:
            absent.setdefault(r["label"], r["excel_row"])
    print()
    print("-" * 74)
    print(f"  {len(absent)} distinct label(s) with nothing close on screen.")
    print("  Each is either a dropped feature or a real regression:")
    for label, row in sorted(absent.items(), key=lambda kv: kv[1])[:30]:
        print(f"      row {row:<5} {label[:60]}")
    if len(absent) > 30:
        print(f"      ... and {len(absent) - 30} more")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
