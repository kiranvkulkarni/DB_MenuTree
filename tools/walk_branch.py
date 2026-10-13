#!/usr/bin/env python3
"""Walk ONE branch of the app, for fixing the walker against a live device.

A full MenuTree run takes up to two hours. When something is wrong on one
screen -- the camera's Settings list, say -- that is two hours per attempt,
and several fixes in a row were each validated only by a full run, so a later
change could quietly undo an earlier one before anyone looked.

This replays a path to one screen and walks only what is beneath it:

    python tools/walk_branch.py --serial <serial> \\
        --path "PORTRAIT,Quick controls,Go to Settings" --time-budget 900

It prints the rows the branch produced, in sheet order, and the walker's
counters, and writes them to output/branch_<timestamp>.json. Nothing about
the walk itself differs from a full run -- same walker, same rules -- so what
works here works there.
"""
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.recursive_walk import RecursiveWalker  # noqa: E402
from src.profile import load_profile  # noqa: E402
from src.run_lock import RunLock  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--package", required=True)
    p.add_argument("--serial", required=True)
    p.add_argument("--path", required=True,
                   help="comma-separated labels from the launch screen down")
    p.add_argument("--time-budget", type=int, default=900)
    p.add_argument("--max-depth", type=int, default=10)
    p.add_argument("--no-reset", action="store_true")
    p.add_argument("--only", default="",
                   help="comma-separated rows of the starting screen to walk; "
                        "the rest is not visited (development aid only)")
    p.add_argument("--skip", default="",
                   help="comma-separated rows to list but not open -- a branch "
                        "already checked on its own (development aid only)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s")
    path = [x.strip() for x in args.path.split(",") if x.strip()]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("output") / f"branch_{stamp}"

    walker = RecursiveWalker(args.package, args.serial, {
        **load_profile(args.package),
        "output_dir": str(out),
        "time_budget": args.time_budget,
        "max_depth": args.max_depth,
        "reset_before_start": not args.no_reset,
        "clear_between_paths": not args.no_reset,
    })

    skip = {x.strip().lower() for x in args.skip.split(",") if x.strip()}
    walker.do_not_press |= skip

    with RunLock(Path("output"), args.serial):
        root_key, views = walker._start()
        if not root_key:
            print("could not read the launch screen")
            return 2
        walker._root_key = root_key
        # The tab strip is learned at the root in a full run; learn it here
        # too, or a mode chip met inside the branch is walked as a menu.
        strip = walker._dedupe(walker._tab_strip(walker._elements(views), views))
        walker._tab_labels = {t.label.strip().lower() for t in strip}

        passed = []
        if not walker._replay(path, passed):
            print(f"could not replay the path: {' > '.join(path)}")
            walker._release()
            return 2
        failure = None
        try:
            # The screens replayed through are this branch's ancestors, as
            # they would be in a full walk; without them a press that drops
            # back to Settings was walked as a new child of the branch.
            only = None
            wanted = {x.strip() for x in args.only.split(",") if x.strip()}
            if wanted:
                _, here, _ = walker._await_stable()
                only = {walker._fingerprint(e)
                        for e in walker._enumerate_scrolled(here or [])
                        if e.label in wanted}
            walker._visit(len(path) + 2, path, [], passed, skip_tabs=True,
                          entering=walker._replayed_last, only=only)
        except Exception as exc:              # keep what was collected
            import traceback
            failure = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            walker._release()

    stats = walker.stats()
    rows = [r.to_dict() for r in walker.rows if r.depth > 1]
    out.mkdir(parents=True, exist_ok=True)
    (out / "branch.json").write_text(
        json.dumps({"path": path, "stats": stats, "rows": rows,
                    "failure": failure}, indent=1),
        encoding="utf-8")

    print()
    print(f"branch: {' > '.join(path)}   ({len(rows)} rows)")
    base = len(path) + 2
    for row in rows:
        indent = "  " * (row["depth"] - base)
        note = f"   [{row['note']}]" if row.get("note") else ""
        print(f"  d{row['depth']:<2}{indent}{row['label'][:50]}{note[:46]}")
    print()
    for key in ("clicks", "scrolls", "nodes_visited", "returns_ok", "returns_failed",
                "step_backs_recovered_by_replay", "toggles_listed_not_switched",
                "listed_but_not_opened", "elapsed_seconds"):
        print(f"  {key:<32}{stats.get(key)}")
    if failure:
        print(f"\n  !! WALK DID NOT COMPLETE: {failure}")
        print("  !! The rows above are PARTIAL -- do not read them as the branch.")
    print(f"\n  written to {out / 'branch.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
