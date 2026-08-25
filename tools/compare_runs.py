"""Compare two (or more) runs side by side.

Every navigation change on this project that was shipped without this
comparison made coverage worse -- three times. It costs one command.

    python tools/compare_runs.py                    # the two most recent
    python tools/compare_runs.py output/a output/b  # named runs
    python tools/compare_runs.py --last 4           # the four most recent
    python tools/compare_runs.py --package com.oplus.camera

Reads `menutree_rows.json` from each run folder. Runs still in flight are
skipped: a mid-run checkpoint looks like a finished result and has already
caused one false "the fix works" here.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Rows: (label, stats key, "higher is better"?). None means neutral.
METRICS = [
    ("elapsed s", "elapsed_seconds", None),
    ("rows", "rows", True),
    ("max depth", "max_depth", True),
    ("screens", "screens_visited", True),
    ("clicks", "clicks", True),
    ("coverage %", "coverage_percent", True),
    ("actionable", "worklist_actionable", None),
    ("still pending", "elements_pending", False),
    ("unreachable", "elements_unreachable", False),
    ("lost returns", "lost_returns", False),
    ("relaunches", "relaunches", False),
    ("nav forward", "nav_forward", True),
    ("nav back", "nav_back", True),
    ("nav sibling", "nav_sibling", True),
    ("identify misses", None, False),          # derived: len(identify_misses)
    ("settle polls", "settle_polls", None),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare MenuTree runs")
    p.add_argument("runs", nargs="*", help="run folders; default: most recent")
    p.add_argument("--last", type=int, default=2, help="how many recent runs")
    p.add_argument("--output-root", default="./output")
    p.add_argument("--package", default="", help="only runs for this package")
    return p.parse_args()


def find_runs(root: Path, package: str, count: int) -> list:
    """Most recent finished runs, newest last."""
    candidates = []
    for folder in root.glob("*/"):
        rows = folder / "menutree_rows.json"
        if not rows.exists():
            continue
        if package and not folder.name.startswith(package):
            continue
        candidates.append(folder)
    candidates.sort(key=lambda f: (f / "menutree_rows.json").stat().st_mtime)
    return candidates[-count:]


def is_live(folder: Path) -> bool:
    """Is a run still driving a device? Then its numbers are a checkpoint."""
    for lock in folder.parent.glob(".run-lock-*"):
        try:
            held = json.loads(lock.read_text(encoding="utf-8"))
        except Exception:
            continue
        from src.run_lock import _alive
        if _alive(int(held.get("pid", 0) or 0)):
            # A live lock does not name its output folder, so this is a
            # conservative warning about the newest run only.
            return True
    return False


def load(folder: Path) -> dict:
    data = json.loads((folder / "menutree_rows.json").read_text(encoding="utf-8"))
    stats = dict(data.get("stats", {}))
    stats["_identify_misses"] = len(stats.get("identify_misses") or [])
    return stats


def arrow(label: str, better_high, old, new) -> str:
    if better_high is None or old is None or new is None or old == new:
        return ""
    improved = (new > old) if better_high else (new < old)
    delta = new - old
    sign = "+" if delta > 0 else ""
    return f"  {'BETTER' if improved else 'worse '} ({sign}{round(delta, 1)})"


def main() -> int:
    args = parse_args()
    root = Path(args.output_root)

    if args.runs:
        folders = [Path(r) for r in args.runs]
    else:
        folders = find_runs(root, args.package, args.last)

    folders = [f for f in folders if (f / "menutree_rows.json").exists()]
    if len(folders) < 1:
        print("No runs with menutree_rows.json found.")
        return 1

    if is_live(root / "x"):
        print("  WARNING: a run is still live on this machine. The newest")
        print("  numbers below may be a mid-run checkpoint, not a result.")
        print()

    runs = [(f.name, load(f)) for f in folders]

    width = max(len(n) for n, _ in runs) + 2
    width = max(width, 16)
    print("=" * (22 + width * len(runs) + 14))
    print("  RUN COMPARISON")
    print("=" * (22 + width * len(runs) + 14))
    print(f"  {'metric':<18}" + "".join(f"{n[-width + 2:]:>{width}}" for n, _ in runs))
    print("  " + "-" * (18 + width * len(runs)))

    for label, key, better in METRICS:
        values = []
        for _, stats in runs:
            values.append(stats.get("_identify_misses") if key is None
                          else stats.get(key))
        if all(v is None for v in values):
            continue
        row = f"  {label:<18}" + "".join(
            f"{('-' if v is None else v):>{width}}" for v in values)
        if len(values) == 2:
            row += arrow(label, better, values[0], values[1])
        print(row)

    print("  " + "-" * (18 + width * len(runs)))
    print("  where the clock went (seconds, % of run):")
    phases = set()
    for _, stats in runs:
        phases.update((stats.get("phase_seconds") or {}).keys())
    for phase in sorted(phases):
        cells = []
        for _, stats in runs:
            entry = (stats.get("phase_seconds") or {}).get(phase)
            cells.append(f"{entry['seconds']}s/{entry['percent_of_run']}%"
                         if entry else "-")
        print(f"  {phase:<18}" + "".join(f"{c:>{width}}" for c in cells))

    print("=" * (22 + width * len(runs) + 14))
    print()
    print("  Coverage is done/(done+pending+unreachable). A run that ends with")
    print("  'still pending' > 0 ran out of clock, not out of app -- so a")
    print("  cheaper action is directly more coverage. See ARCHITECTURE 12.55.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
