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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.action_guard import DEFAULT_PRESETS, GUARD_PRESETS  # noqa: E402
from src.crawler.element_tree import ElementTreeWalker  # noqa: E402
from src.generator import tree_uvta  # noqa: E402
from src.generator.menutree_sheet import summarise, write_csv  # noqa: E402
from src.generator.menutree_workbook import write_workbook  # noqa: E402
from src.generator.uvta_writer import SuiteValidationError, UVTAWriter  # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402

logger = logging.getLogger("build_menutree")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MenuTree element-tree walker")
    p.add_argument("--package", required=True)
    p.add_argument("--serial", default=None)
    p.add_argument("--output-root", default="./output",
                   help="Parent folder. Each run gets its own subfolder "
                        "named <package>_<YYYYMMDD_HHMMSS_mmm>, so runs "
                        "never overwrite each other.")
    p.add_argument("--output-dir", default=None,
                   help="Write straight into this folder instead of "
                        "creating a per-run one. Required with --skip-walk "
                        "to point at an existing run.")
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
    p.add_argument("--no-reset", action="store_true",
                   help="Do NOT pm clear the app before exploring. By default each run starts from a fresh app state, which also brings back "
                        "first-run pop-ups a previous run dismissed.")
    p.add_argument("--no-guard", action="store_true")
    p.add_argument("--guard-presets", default=",".join(DEFAULT_PRESETS),
                   help=f"available: {', '.join(sorted(GUARD_PRESETS))}")
    p.add_argument("--guard-extra", default="")
    p.add_argument(
        "--clear-between-paths", action="store_true",
        help="pm clear the app before a relaunch when a sibling element has "
             "vanished (the one-shot-dialog case: an earlier option already "
             "dismissed it). Required to reach BOTH branches of a dialog like "
             "'Turn on Location tags? Cancel / Turn on'. Resets the app's "
             "saved preferences every time it fires -- only use on a "
             "disposable test device, never on one with real user data.",
    )
    p.add_argument("--uvta-output", default=None,
                   help="Path for the UVTA suite. Defaults to "
                        "<output-dir>/<package>_suite.uvta.")
    p.add_argument("--no-uvta", action="store_true",
                   help="Write only the sheet, no UVTA suite.")
    p.add_argument("--no-xlsx", action="store_true",
                   help="Skip the Excel workbook (CSV is always written).")
    p.add_argument("--skip-walk", action="store_true",
                   help="Re-export an existing menutree_rows.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        now = datetime.now()
        stamp = f"{now:%Y%m%d_%H%M%S}_{now.microsecond // 1000:03d}"
        output_dir = Path(args.output_root) / f"{args.package}_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    run_id = output_dir.name
    setup_logging(log_dir=str(output_dir), level="INFO", run_id=run_id)
    logger.info("Run folder: %s", output_dir.resolve())
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
            "clear_between_paths": args.clear_between_paths,
            "reset_before_start": not args.no_reset,
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
        print(f"  dialog recoveries: {stats.get('dialog_recoveries')} "
              f"(clear_between_paths={stats.get('clear_between_paths')})")
        print(f"  elapsed         : {stats.get('elapsed_seconds')}s")
        print("-" * 60)
        print(f"  COVERAGE        : {stats.get('coverage_percent')}%  "
              f"({stats.get('worklist_by_status', {}).get('done', 0)}"
              f"/{stats.get('worklist_actionable')} actionable elements)")
        print(f"  still pending   : {stats.get('elements_pending')}")
        print(f"  unreachable     : {stats.get('elements_unreachable')}")
        print(f"  worklist status : {stats.get('worklist_by_status')}")
        guard = stats.get("guard") or {}
        if guard.get("blocked_attempts"):
            print(f"  guard blocked   : {guard['blocked_attempts']} on "
                  f"{len(guard.get('blocked_controls', []))} control(s)")
        print("=" * 60)

    write_csv(rows, output_dir / "menutree.csv", max_depth=args.max_depth)

    cases, uvta_by_row = ([], {})
    if not args.no_uvta:
        cases, uvta_by_row = tree_uvta.emit_indexed(rows, args.package)

    if not args.no_xlsx:
        write_workbook(
            rows,
            output_dir / f"{args.package}_menutree.xlsx",
            package=args.package,
            max_depth=args.max_depth,
            uvta_by_row=uvta_by_row,
            stats=stats,
        )
        print(f"  Workbook        : "
              f"{(output_dir / f'{args.package}_menutree.xlsx').resolve()}")

    if not args.no_uvta:
        if cases:
            uvta_path = Path(
                args.uvta_output or (output_dir / f"{args.package}_suite.uvta")
            )
            try:
                UVTAWriter({"uvta_output": str(uvta_path)}).write_suite(
                    cases,
                    header_comment="\n".join([
                        "# Generated by MenuTree AutoQA from the element tree.",
                        "# One test per row: navigate the path, then assert",
                        "# the item is present.",
                        f"# Package: {args.package}",
                        f"# Testcases: {len(cases)}",
                    ]),
                )
                print(f"  UVTA suite      : {uvta_path.resolve()} ({len(cases)} cases)")
            except SuiteValidationError as exc:
                logger.error("UVTA suite rejected: %s", exc)
        else:
            logger.warning("No rows were eligible for UVTA emission.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
