"""Run the replay explorer and compare it against the DroidBot baseline.

    python tools/explore_u2.py --package com.jewelestimate.app \
        --serial emulator-5554 --time-budget 600 \
        --compare ./droidbot_out/jewel

Prints a side-by-side of what each crawler discovered, then emits the UVTA
suite from the explorer's graph so the whole path is exercised end to end.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.coverage import CoverageAnalyzer  # noqa: E402
from src.crawler.action_guard import DEFAULT_PRESETS, GUARD_PRESETS  # noqa: E402
from src.crawler.replay_explorer import ReplayExplorer  # noqa: E402
from src.generator.path_emitter import PathEmitter  # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402
from src.parser.menutree_loader import MenuTreeLoader  # noqa: E402
from src.parser.utg_parser import UTGParser  # noqa: E402

logger = logging.getLogger("explore_u2")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="uiautomator2 replay explorer")
    p.add_argument("--package", required=True)
    p.add_argument("--serial", default=None)
    p.add_argument("--output-dir", default="./u2_out")
    p.add_argument("--time-budget", type=float, default=600)
    p.add_argument("--max-states", type=int, default=300)
    p.add_argument("--max-actions", type=int, default=3000)
    p.add_argument("--max-depth", type=int, default=8)
    p.add_argument("--settle", type=float, default=1.0)
    p.add_argument(
        "--state-mode", default="affordance",
        choices=("affordance", "structure", "content"),
    )
    p.add_argument("--driver", default="auto", choices=("auto", "u2", "adb"))
    p.add_argument("--no-screenshots", action="store_true")
    p.add_argument(
        "--no-clear-between-paths", action="store_true",
        help="Skip `pm clear` before each replay. Faster, but any one-time UI "
             "the app records as dismissed makes the recorded root unreachable "
             "and the crawl silently collapses. Measured on the Phone app: "
             "71/74 replays drifted without clearing.",
    )
    p.add_argument("--ready-timeout", type=float, default=12.0)
    p.add_argument(
        "--no-guard", action="store_true",
        help="Disable the destructive-action guard. The crawler may then "
             "delete data or send it off the device. Never use on a device "
             "holding real data.",
    )
    p.add_argument(
        "--guard-presets", default=",".join(DEFAULT_PRESETS),
        help="Comma-separated guard presets to apply "
             f"(available: {', '.join(sorted(GUARD_PRESETS))}).",
    )
    p.add_argument(
        "--guard-extra", default="",
        help="Comma-separated extra regex patterns to block, matched "
             "case-insensitively on word boundaries against the control label.",
    )
    p.add_argument(
        "--compare", default=None,
        help="A droidbot_out directory to compare discovery against",
    )
    p.add_argument("--skip-explore", action="store_true",
                   help="Reuse an existing menutree.json")
    return p.parse_args()


def summarise(label: str, tree, extra: str = "") -> dict:
    s = tree.summary()
    return {
        "label": label,
        "states": s["states"],
        "transitions": s["transitions"],
        "actionable": s["actionable_transitions"],
        "ambiguous": s["ambiguous_transitions"],
        "unreachable": s["unreachable_states"],
        "extra": extra,
    }


def main() -> int:
    args = parse_args()
    setup_logging(log_dir="./logs", level="INFO", run_id="u2_explore")

    output_dir = Path(args.output_dir)
    config = {
        "output_dir": str(output_dir),
        "time_budget": args.time_budget,
        "max_states": args.max_states,
        "max_actions": args.max_actions,
        "max_depth": args.max_depth,
        "settle_seconds": args.settle,
        "state_key_mode": args.state_mode,
        "capture_screenshots": not args.no_screenshots,
        "driver": args.driver,
        "clear_between_paths": not args.no_clear_between_paths,
        "ready_timeout": args.ready_timeout,
        "guard_enabled": not args.no_guard,
        "guard_presets": [p for p in args.guard_presets.split(",") if p],
        "guard_extra_patterns": [p for p in args.guard_extra.split(",") if p],
    }

    if not args.skip_explore:
        explorer = ReplayExplorer(args.package, args.serial, config)
        try:
            explorer.explore()
        except (KeyboardInterrupt, Exception) as exc:
            # The device can die mid-crawl. Keep whatever was discovered and
            # report on it rather than discarding a long run.
            logger.error("Exploration ended early: %s", exc)
            if not explorer.result.states:
                raise
            explorer._finalise_stats(f"aborted: {type(exc).__name__}", 0)
        explorer.write()

    tree = MenuTreeLoader({"output_dir": str(output_dir)}).load()
    emitter = PathEmitter({}, args.package)
    cases = emitter.emit(tree)

    rows = [summarise("uiautomator2 replay", tree,
                      f"stop={tree.meta.get('stop_reason', '?')}")]

    if args.compare:
        try:
            baseline = UTGParser({"output_dir": args.compare}).parse()
            rows.append(summarise("DroidBot dfs_greedy", baseline,
                                  f"{baseline.meta.get('time_spent', 0):.0f}s crawl"))
        except Exception as exc:
            logger.warning("Could not parse comparison baseline: %s", exc)

    print()
    print("=" * 78)
    print("  CRAWLER COMPARISON")
    print("=" * 78)
    print(f"  {'crawler':<24} {'states':>7} {'edges':>7} {'actionable':>11} "
          f"{'ambig':>6} {'unreach':>8}")
    print("  " + "-" * 74)
    for r in rows:
        print(f"  {r['label']:<24} {r['states']:>7} {r['transitions']:>7} "
              f"{r['actionable']:>11} {r['ambiguous']:>6} {r['unreachable']:>8}")
    for r in rows:
        if r["extra"]:
            print(f"    {r['label']}: {r['extra']}")
    print("=" * 78)
    guard = tree.meta.get("guard") or {}
    if guard.get("blocked_attempts"):
        print(f"  Guard blocked {guard['blocked_attempts']} attempt(s) on "
              f"{len(guard.get('blocked_controls', []))} control(s) "
              "-- KNOWN coverage gap:")
        for control in guard.get("blocked_controls", [])[:15]:
            print(f"      - {control}")
    print(f"  Testcases emitted from the u2 graph: {len(cases)}")
    if emitter.skipped:
        print(f"  Skipped: {len(emitter.skipped)}")
    print("=" * 78)

    analyzer = CoverageAnalyzer({"report_dir": "./reports"})
    report = analyzer.analyze(tree, "u2_explore", len(cases), emitter.skipped)
    analyzer.write(report, Path("./reports/u2_explore.json"))

    if cases:
        print()
        print("--- sample testcases ---")
        for case in cases[:3]:
            print()
            print(case.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
