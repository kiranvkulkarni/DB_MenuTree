"""Verify a build against the hand-authored MenuTree specification.

Walks the *expected* tree rather than discovering one, which is what makes
this usable as a gate:

* **Reproducible.** The same rows are visited in the same order every run,
  because the order comes from the sheet, not from what the crawler happened
  to find. Discovery produced 90-465 rows from identical code; this cannot.
* **Fixed denominator.** 1,896 rows, known before the run starts, so a pass
  rate means something on day one.
* **Failures are named.** A row that cannot be reached is a Fail against a
  specific line of the specification, not silently missing coverage.

The sheet is already in depth-first order, so walking it top to bottom is
mostly local movement: the next row is usually a sibling or a child of the
one just checked. Navigation cost -- the thing that dominated discovery --
largely disappears.
"""
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..crawler.action_guard import DEFAULT_PRESETS, ActionGuard
from ..crawler.device_driver import DeviceDriver, DriverError, make_driver
from ..crawler.elements import Element, enumerate_elements, screen_similarity
from ..crawler.hierarchy import EMPTY_STATE, center_of, parse_hierarchy, state_key
from .spec_reader import SpecRow

logger = logging.getLogger(__name__)

PASS = "Pass"
FAIL = "Fail"
NA = "NA"
NOT_TESTED = "NT"


@dataclass
class RowResult:
    row: SpecRow
    result: str = NOT_TESTED
    detail: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "sheet": self.row.sheet,
            "excel_row": self.row.excel_row,
            "depth": self.row.depth,
            "label": self.row.label,
            "selector_text": self.row.selector_text,
            "path": self.row.path,
            "context": self.row.context,
            "result": self.result,
            "detail": self.detail,
            "elapsed": round(self.elapsed, 2),
        }


@dataclass
class VerifyReport:
    package: str
    results: List[RowResult] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.results:
            out[r.result] = out.get(r.result, 0) + 1
        return dict(sorted(out.items()))

    def pass_rate(self) -> float:
        judged = [r for r in self.results if r.result in (PASS, FAIL)]
        if not judged:
            return 0.0
        passed = sum(1 for r in judged if r.result == PASS)
        return round(100.0 * passed / len(judged), 2)


class MenuTreeVerifier:
    def __init__(self, package: str, serial: Optional[str], config: dict):
        self.package = package
        self.serial = serial

        self.settle = float(config.get("settle_seconds", 1.0))
        self.ready_timeout = float(config.get("ready_timeout", 20.0))
        self.stable_interval = float(config.get("stable_interval", 0.4))
        self.state_mode = config.get("state_key_mode", "affordance")
        self.backend = config.get("driver", "auto")
        self.return_similarity = float(config.get("return_similarity", 0.75))
        self.reset_before_start = bool(config.get("reset_before_start", True))
        self.time_budget = float(config.get("time_budget", 7200))
        self.stop_on_missing_path = bool(config.get("stop_on_missing_path", False))

        self.guard = ActionGuard.from_config(
            enabled=config.get("guard_enabled", True),
            presets=config.get("guard_presets", DEFAULT_PRESETS),
            extra=config.get("guard_extra_patterns") or [],
        )

        self.driver: Optional[DeviceDriver] = None
        self._started = 0.0
        self._current_path: List[str] = []
        self._navigations = 0
        self._relaunches = 0

    # -- device ----------------------------------------------------------
    def _capture(self) -> Tuple[Optional[str], List[Dict], str]:
        assert self.driver is not None
        views = parse_hierarchy(self.driver.dump_hierarchy())
        if not views:
            return None, [], ""
        return (state_key(views, self.state_mode, self.package),
                views, self.driver.current_package() or "")

    def _await_stable(self) -> Tuple[Optional[str], List[Dict], str]:
        deadline = time.time() + self.ready_timeout
        previous, views, current = self._capture()
        settled = None
        while time.time() < deadline:
            if previous and previous != EMPTY_STATE and settled == previous:
                return previous, views, current
            settled = previous
            time.sleep(self.stable_interval)
            previous, views, current = self._capture()
        return previous, views, current

    def _elements(self, views: Sequence[Dict]) -> List[Element]:
        return enumerate_elements(views, self.package)

    def _find(self, text: str, views: Sequence[Dict]) -> Optional[Element]:
        """Locate an element whose visible text matches the specification."""
        if not text:
            return None
        wanted = text.strip().lower()
        elements = self._elements(views)
        for element in elements:
            if element.label.strip().lower() == wanted:
                return element
        # The sheet is hand-authored, so wording drifts: trailing state
        # suffixes, punctuation, truncation. Accept containment either way
        # before declaring a row missing.
        for element in elements:
            label = element.label.strip().lower()
            if label and (wanted in label or label in wanted):
                return element
        return None

    def _click(self, element: Element, views: Sequence[Dict]) -> bool:
        assert self.driver is not None
        if element.view_index >= len(views):
            return False
        centre = center_of(views[element.view_index])
        if centre is None:
            return False
        try:
            self.driver.tap(*centre)
        except DriverError:
            return False
        return True

    def _relaunch(self) -> None:
        assert self.driver is not None
        self._relaunches += 1
        if not self.driver.launch_clean(self.package, clear=False):
            self.driver.start_app(self.package, clear=False)
        self._current_path = []

    # -- navigation ------------------------------------------------------
    def _navigate(self, path: Sequence[str]) -> Tuple[bool, str]:
        """Put the device on the screen the given path describes.

        Exploits the sheet's depth-first order: consecutive rows usually
        share a prefix, so the common case is to walk forward a step or two
        from where we already are rather than restart.
        """
        assert self.driver is not None
        path = list(path)

        shared = 0
        for a, b in zip(self._current_path, path):
            if a != b:
                break
            shared += 1

        if shared < len(self._current_path):
            # We are deeper than, or off, the target path: restart and replay.
            self._relaunch()
            shared = 0

        self._current_path = path[:shared]
        for step in range(shared, len(path)):
            label = path[step]
            self._navigations += 1

            blocked = self.guard.blocks("text", label)
            if blocked:
                return False, f"path step blocked by action guard ({blocked})"

            _, views, _ = self._await_stable()
            if not views:
                return False, "no readable screen while navigating"
            element = self._find(label, views)
            if element is None:
                return False, f"path step not found on screen: {label!r}"
            if not self._click(element, views):
                return False, f"path step could not be clicked: {label!r}"

            # Track position by index, not by searching for the label: the
            # sheet repeats labels ("ON", "Back key icon", "OK") at many
            # points in the tree, so index(label) would resolve to the wrong
            # depth and corrupt the shared-prefix calculation for every
            # subsequent row.
            self._current_path = path[:step + 1]
        return True, ""

    # -- verify ----------------------------------------------------------
    def verify(self, spec: Sequence[SpecRow]) -> VerifyReport:
        report = VerifyReport(package=self.package)
        self.driver = make_driver(self.serial, self.backend, self.settle)
        self._started = time.time()

        if not self.driver.launch_clean(self.package, clear=self.reset_before_start):
            self.driver.start_app(self.package, clear=self.reset_before_start)
        self._current_path = []

        for index, row in enumerate(spec, start=1):
            if time.time() - self._started > self.time_budget:
                logger.warning("Time budget reached with %d row(s) unchecked.",
                               len(spec) - index + 1)
                break

            started = time.time()
            result = self._verify_row(row)
            result.elapsed = time.time() - started
            report.results.append(result)

            if index % 25 == 0:
                counts = report.counts()
                logger.info("%d/%d checked  %s", index, len(spec), counts)

        report.stats = {
            "rows_in_spec": len(spec),
            "rows_checked": len(report.results),
            "counts": report.counts(),
            "pass_rate_percent": report.pass_rate(),
            "navigations": self._navigations,
            "relaunches": self._relaunches,
            "elapsed_seconds": round(time.time() - self._started, 1),
            "guard": self.guard.summary(),
        }
        logger.info("Verification finished: %s", report.stats)
        return report

    def _verify_row(self, row: SpecRow) -> RowResult:
        # A bracketed context row states a precondition; there is nothing on
        # screen to assert.
        if row.is_context:
            return RowResult(row, NA, "context/precondition row -- not verifiable")

        # Depth 1 is the application itself. It is satisfied by the app being
        # up, which the launch already established.
        if row.is_root:
            _, views, current = self._await_stable()
            if current == self.package and views:
                return RowResult(row, PASS, "application launched")
            return RowResult(row, FAIL, f"app not in foreground (saw {current!r})")

        if row.context:
            return RowResult(
                row, NA,
                "requires precondition: " + "; ".join(row.context),
            )

        blocked = self.guard.blocks("text", row.selector_text)
        if blocked:
            return RowResult(
                row, NA,
                f"withheld by action guard ({blocked}) -- verify manually",
            )

        reached, why = self._navigate(row.path)
        if not reached:
            return RowResult(row, FAIL, why)

        _, views, current = self._await_stable()
        if not views:
            return RowResult(row, FAIL, "no readable screen")
        if current and current != self.package:
            return RowResult(
                row, NA, f"screen belongs to {current} -- outside the app",
            )

        element = self._find(row.selector_text, views)
        if element is None:
            return RowResult(
                row, FAIL,
                f"expected element not present: {row.selector_text!r}",
            )
        return RowResult(row, PASS, f"found as {element.kind}")
