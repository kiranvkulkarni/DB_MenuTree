"""Walk an app's element tree and export it in the workbook's layout.

    python tools/build_menutree.py --package com.sec.android.app.camera \
        --serial <serial> --time-budget 3600 --max-depth 18

Produces tree_out/menutree_rows.json (full detail) and
tree_out/menutree.csv (depth-column layout, opens in Excel).
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.action_guard import DEFAULT_PRESETS, GUARD_PRESETS  # noqa: E402
from src.crawler.element_tree import ElementTreeWalker  # noqa: E402
from src.generator.menutree_sheet import summarise, write_csv  # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402

logger = logging.getLogger("build_menutree")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MenuTree element-tree walker")
    p.add_argument("--package", required=True)
    p.add_argument("--serial", default=None)
    p.add_argument("--output-dir", default="./tree_out")
    p.add_argument("--time-budget", type=float, default=3600)
    p.add_argument("--max-depth", type=int, default=18)
    p.add_argument("--ready-timeout", type=float, default=25.0)
    p.add_argument("--settle", type=float, default=1.0)
    p.add_argument("--driver", default="auto", choices=("auto", "u2", "adb"))
    p.add_argument(
        "--similarity-threshold", type=float, default=0.6,
        help="Below this label overlap a click counts as opening a submenu; "
             "above it, as merely selecting an option on the same screen.",
    )
    p.add_argument(
        "--no-static-text", action="store_true",
        help="Skip titles, subtitles and descriptive text. The expected sheet "
             "includes them, so this loses rows.",
    )
    p.add_argument(
        "--no-foreign", action="store_true",
        help="Skip elements from other packages. The OS permission dialog "
             "(Precise / Approximate / Only this time) belongs to the "
             "permission controller and appears in the expected sheet, so "
             "this loses real coverage.",
    )
    p.add_argument("--no-guard", action="store_true")
    p.add_argument("--guard-presets", default=",".join(DEFAULT_PRESETS),
                   help=f"available: {', '.join(sorted(GUARD_PRESETS))}")
    p.add_argument("--guard-extra", default="")
    p.add_argument("--skip-walk", action="store_true",
                   help="Re-export an existing menutree_rows.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(log_dir="./logs", level="INFO", run_id="menutree")
    output_dir = Path(args.output_dir)
    rows_file = output_dir / "menutree_rows.json"

    if not args.skip_walk:
        walker = ElementTreeWalker(args.package, args.serial, {
            "output_dir": str(output_dir),
            "time_budget": args.time_budget,
            "max_depth": args.max_depth,
            "ready_timeout": args.ready_timeout,
            "settle_seconds": args.settle,
            "driver": args.driver,
            "similarity_threshold": args.similarity_threshold,
            "include_static_text": not args.no_static_text,
            "include_foreign": not args.no_foreign,
            "guard_enabled": not args.no_guard,
            "guard_presets": [g for g in args.guard_presets.split(",") if g],
            "guard_extra_patterns": [g for g in args.guard_extra.split(",") if g],
        })
        try:
            walker.walk()
        except KeyboardInterrupt:
            logger.warning("Interrupted; keeping what was discovered.")
        except Exception as exc:
            logger.error("Walk ended early: %s", exc)
            if not walker.rows:
                raise
        walker.write(rows_file)

    if not rows_file.exists():
        logger.error("No rows file at %s", rows_file)
        return 1

    data = json.loads(rows_file.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    if not rows:
        logger.error("No rows discovered.")
        return 1

    print(summarise(rows))
    stats = data.get("stats", {})
    if stats:
        print(f"  screens visited : {stats.get('screens_visited')}")
        print(f"  clicks          : {stats.get('clicks')}")
        print(f"  descents        : {stats.get('descents')}")
        print(f"  BACK ok/failed  : {stats.get('back_ok')}/{stats.get('back_failed')}")
        print(f"  relaunches      : {stats.get('relaunches')}")
        print(f"  elapsed         : {stats.get('elapsed_seconds')}s")
        guard = stats.get("guard") or {}
        if guard.get("blocked_attempts"):
            print(f"  guard blocked   : {guard['blocked_attempts']} on "
                  f"{len(guard.get('blocked_controls', []))} control(s)")
        print("=" * 60)

    write_csv(rows, output_dir / "menutree.csv", max_depth=args.max_depth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
