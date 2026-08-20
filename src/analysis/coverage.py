"""Coverage measurement and baseline diffing for the MenuTree gate.

Answers the only question a release gate cares about: did we reach everything,
and did we reach *less* than last time?

The denominator comes free from the APK manifest -- DroidBot's App class
already parses the declared activity list -- so activity coverage is a real
percentage, not a guess.
"""
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..parser.menu_tree import MenuTree

logger = logging.getLogger(__name__)


@dataclass
class CoverageReport:
    package: str = ""
    run_id: str = ""
    test_date: str = ""
    time_spent_seconds: float = 0.0

    activities_declared: int = 0
    activities_reached: int = 0
    activities_testable: int = 0
    activities_missing: List[str] = field(default_factory=list)
    activities_reached_names: List[str] = field(default_factory=list)
    activities_testable_names: List[str] = field(default_factory=list)
    activities_discovered_not_testable: List[str] = field(default_factory=list)

    states_discovered: int = 0
    transitions_discovered: int = 0
    actionable_transitions: int = 0
    ambiguous_transitions: int = 0
    unidentified_transitions: int = 0
    unreachable_states: int = 0
    dead_end_states: int = 0

    testcases_emitted: int = 0
    transitions_skipped: List[str] = field(default_factory=list)

    @property
    def activity_coverage_pct(self) -> float:
        """Crawl breadth: activities the crawler physically reached."""
        if not self.activities_declared:
            return 0.0
        return round(100.0 * self.activities_reached / self.activities_declared, 2)

    @property
    def testable_coverage_pct(self) -> float:
        """The number the gate uses: activities a generated test can reach."""
        if not self.activities_declared:
            return 0.0
        return round(100.0 * self.activities_testable / self.activities_declared, 2)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["activity_coverage_pct"] = self.activity_coverage_pct
        data["testable_coverage_pct"] = self.testable_coverage_pct
        return data


class CoverageAnalyzer:
    def __init__(self, config: dict):
        self.declared_activities: Optional[List[str]] = None
        self.apk_path = config.get("apk_path")
        self.baseline_file = config.get("baseline_file")
        self.min_activity_coverage = float(config.get("min_activity_coverage", 0.0))
        self.fail_on_regression = config.get("fail_on_regression", True)

    # -- build -----------------------------------------------------------
    def analyze(
        self, tree: MenuTree, run_id: str, testcases_emitted: int, skipped: List[str]
    ) -> CoverageReport:
        reached = tree.reached_activities()
        testable = tree.testable_activities()
        declared = self._declared_activities(tree)

        report = CoverageReport(
            package=tree.meta.get("app_package", ""),
            run_id=run_id,
            test_date=tree.meta.get("test_date", ""),
            time_spent_seconds=float(tree.meta.get("time_spent") or 0.0),
            activities_reached=len(reached),
            activities_reached_names=reached,
            activities_testable=len(testable),
            activities_testable_names=testable,
            activities_discovered_not_testable=sorted(set(reached) - set(testable)),
            states_discovered=len(tree.states),
            transitions_discovered=len(tree.transitions),
            actionable_transitions=len(tree.actionable_transitions()),
            ambiguous_transitions=len(tree.ambiguous_transitions()),
            unidentified_transitions=len(tree.unidentified_transitions()),
            unreachable_states=len(tree.unreachable_states()),
            dead_end_states=len(tree.dead_end_states()),
            testcases_emitted=testcases_emitted,
            transitions_skipped=skipped,
        )

        if declared is not None:
            report.activities_declared = len(declared)
            report.activities_missing = sorted(set(declared) - set(reached))
        else:
            # No manifest available: fall back to DroidBot's own count so the
            # percentage is still meaningful, but we cannot name what is missing.
            report.activities_declared = int(
                tree.meta.get("app_num_total_activities") or 0
            )
            logger.warning(
                "Manifest activity list unavailable; cannot name unreached "
                "activities. Set generator.apk_path (or install androguard) to "
                "get the exact missing-screen list."
            )
        return report

    def _declared_activities(self, tree: MenuTree) -> Optional[List[str]]:
        if self.declared_activities is not None:
            return self.declared_activities
        if not self.apk_path or not Path(self.apk_path).exists():
            return None
        try:
            from androguard.core.apk import APK
        except ImportError:
            try:
                from androguard.core.bytecodes.apk import APK  # type: ignore
            except ImportError:
                logger.warning("androguard not installed; skipping manifest parse.")
                return None
        try:
            self.declared_activities = list(APK(self.apk_path).get_activities())
        except Exception as exc:
            logger.warning("Could not parse activities from %s: %s", self.apk_path, exc)
            return None
        return self.declared_activities

    # -- persistence & gating --------------------------------------------
    def write(self, report: CoverageReport, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        logger.info("Coverage report written: %s", path.resolve())

    def load_baseline(self) -> Optional[dict]:
        if not self.baseline_file:
            return None
        baseline_path = Path(self.baseline_file)
        if not baseline_path.exists():
            logger.info("No baseline at %s; this run establishes one.", baseline_path)
            return None
        try:
            return json.loads(baseline_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Baseline %s unreadable: %s", baseline_path, exc)
            return None

    def evaluate_gate(self, report: CoverageReport) -> List[str]:
        """Return a list of gate failures. Empty list == pass."""
        failures: List[str] = []

        # Gate on TESTABLE coverage, not crawl breadth: an activity the crawler
        # stumbled into but that no emitted testcase can reach is not covered.
        if self.min_activity_coverage and report.activities_declared:
            if report.testable_coverage_pct < self.min_activity_coverage:
                missing = report.activities_missing or ["unknown"]
                failures.append(
                    f"Testable activity coverage {report.testable_coverage_pct}% is "
                    f"below the required {self.min_activity_coverage}%. "
                    f"Unreached: {', '.join(missing[:10])}"
                )

        if report.activities_discovered_not_testable:
            failures.append(
                f"{len(report.activities_discovered_not_testable)} activity/activities "
                "were discovered by the crawler but no testcase can reach them: "
                + ", ".join(report.activities_discovered_not_testable[:10])
            )

        baseline = self.load_baseline()
        if baseline and self.fail_on_regression:
            failures.extend(self._regressions(report, baseline))
        return failures

    @staticmethod
    def _regressions(report: CoverageReport, baseline: dict) -> List[str]:
        failures: List[str] = []

        prev_reached = set(baseline.get("activities_testable_names", []))
        now_reached = set(report.activities_testable_names)
        lost = sorted(prev_reached - now_reached)
        if lost:
            failures.append(
                f"REGRESSION: {len(lost)} activity/activities reachable in the "
                f"baseline are now unreachable: {', '.join(lost[:10])}"
            )

        for field_name, label in (
            ("states_discovered", "states"),
            ("actionable_transitions", "actionable transitions"),
            ("testcases_emitted", "emitted testcases"),
            ("activities_testable", "testable activities"),
        ):
            prev = baseline.get(field_name, 0)
            now = getattr(report, field_name)
            if prev and now < prev:
                failures.append(
                    f"REGRESSION: {label} dropped from {prev} to {now} "
                    f"({prev - now} fewer than baseline)."
                )
        return failures

    def summarize(self, report: CoverageReport) -> str:
        lines = [
            "",
            "=" * 62,
            "  MENUTREE COVERAGE REPORT",
            "=" * 62,
            f"  Package                : {report.package}",
            f"  Run                    : {report.run_id}",
            f"  Crawl duration         : {report.time_spent_seconds:.0f}s",
            "-" * 62,
            f"  Testable coverage      : {report.testable_coverage_pct}%  "
            f"({report.activities_testable}/{report.activities_declared})   <-- GATED",
            f"  Crawl breadth          : {report.activity_coverage_pct}%  "
            f"({report.activities_reached}/{report.activities_declared})",
            f"  States discovered      : {report.states_discovered}",
            f"  Transitions discovered : {report.transitions_discovered}",
            f"  Actionable transitions : {report.actionable_transitions}",
            f"  Ambiguous (class)      : {report.ambiguous_transitions}"
            "   <-- found but not addressable",
            f"  Unidentified controls  : {report.unidentified_transitions}",
            f"  Unreachable states     : {report.unreachable_states}",
            f"  Dead-end states        : {report.dead_end_states}",
            f"  Testcases emitted      : {report.testcases_emitted}",
            f"  Transitions skipped    : {len(report.transitions_skipped)}",
        ]
        if report.activities_discovered_not_testable:
            lines.append("-" * 62)
            lines.append("  DISCOVERED BUT NOT TESTABLE (no path from launch):")
            for activity in report.activities_discovered_not_testable[:25]:
                lines.append(f"    - {activity}")
        if report.activities_missing:
            lines.append("-" * 62)
            lines.append(f"  UNREACHED ACTIVITIES ({len(report.activities_missing)}):")
            for activity in report.activities_missing[:25]:
                lines.append(f"    - {activity}")
            if len(report.activities_missing) > 25:
                lines.append(f"    ... and {len(report.activities_missing) - 25} more")
        lines.append("=" * 62)
        return "\n".join(lines)
