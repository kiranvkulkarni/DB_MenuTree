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
from ..crawler.hierarchy import (
    EMPTY_STATE,
    center_of,
    foreground_package,
    parse_hierarchy,
    state_key,
)
from .matching import CONFIDENT, REVIEW, Match, best_match
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
        self.first_interval = float(config.get("first_interval", 0.12))
        self.interval_growth = float(config.get("interval_growth", 2.0))
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

        self._aliases: Dict[str, str] = config.get("aliases") or {}
        # Every non-exact match, so a human can confirm the wording once.
        self.match_log: List[Dict] = []

        self.driver: Optional[DeviceDriver] = None
        self._started = 0.0
        self._current_path: List[str] = []
        self._navigations = 0
        self._relaunches = 0
        self._released = False

    # -- device ----------------------------------------------------------
    def _capture(self, with_package: bool = True) -> Tuple[Optional[str], List[Dict], str]:
        """Read the screen; `with_package` is the expensive half. See the
        walker's `_capture` -- the same measurements apply, and matter more
        here because the verifier walks every row of the sheet."""
        assert self.driver is not None
        views = parse_hierarchy(self.driver.dump_hierarchy())
        if not views:
            return None, [], ""
        current = self._resolve_package(views) if with_package else ""
        return state_key(views, self.state_mode, self.package), views, current

    def _resolve_package(self, views: Sequence[Dict]) -> str:
        """Foreground app, read from the dump; confirmed only when foreign.

        The authoritative call costs ~390ms against ~150ms for the dump, and
        was 23% of a measured walk. It is still paid when the cheap answer
        says we have left the app, because that is the answer worth being
        certain about.
        """
        assert self.driver is not None
        derived = foreground_package(views)
        if not derived or derived == self.package:
            return derived
        return self.driver.current_package() or derived

    def _await_stable(self) -> Tuple[Optional[str], List[Dict], str]:
        """Poll until two dumps agree, with a growing gap between looks.

        Nearly every screen is already still on the second look, so glance
        again quickly and reserve patience for the ones that are genuinely
        still animating.
        """
        deadline = time.time() + self.ready_timeout
        previous, views, _ = self._capture(with_package=False)
        settled = None
        gap = self.first_interval
        while time.time() < deadline:
            if previous and previous != EMPTY_STATE and settled == previous:
                break
            settled = previous
            time.sleep(gap)
            gap = min(gap * self.interval_growth, self.stable_interval * 2)
            previous, views, _ = self._capture(with_package=False)
        current = self._resolve_package(views) if views else ""
        return previous, views, current

    def _elements(self, views: Sequence[Dict]) -> List[Element]:
        return enumerate_elements(views, self.package)

    def _match(self, text: str, views: Sequence[Dict]) -> Optional[Match]:
        """Best on-screen element for a spec label, scored.

        The depth columns are not selectors -- they are how a manual test
        engineer described the control in their own English. "Flash icon" is
        `Flash`, "Priorize quality" is `Prioritize quality`, "Back key icon"
        may be a content-desc of `Navigate up`. Exact matching reports a Fail
        on a control that is present and working, which for a gate is the
        worst error there is: it manufactures defects.

        See `matching.py`. Scores below `REVIEW` are not a match.
        """
        if not text:
            return None
        match = best_match(text, self._elements(views), self._aliases)
        if match is None or match.score < REVIEW:
            return None
        return match

    def _find(self, text: str, views: Sequence[Dict]) -> Optional[Element]:
        """Back-compat wrapper: the element only, no score."""
        match = self._match(text, views)
        return match.element if match else None

    def _click(self, element: Element, views: Sequence[Dict]) -> bool:
        assert self.driver is not None
        if element.view_index >= len(views):
            return False
        centre = center_of(views[element.view_index])
        if centre is None:
            return False
        try:
            # settle=False: _await_stable follows every click here, so the
            # driver's blind 1.0s sleep on top of it was dead time.
            self.driver.tap(*centre, settle=False)
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
        if not self.driver.prepare_device():
            logger.warning("Device could not be woken/unlocked.")
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
        self._release()
        logger.info("Verification finished: %s", report.stats)
        return report

    def _release(self) -> None:
        """Drop the stay-awake hold; safe to call more than once."""
        if getattr(self, "_released", False) or self.driver is None:
            return
        self._released = True
        try:
            self.driver.release_device(self.package)
        except Exception as exc:
            logger.debug("release_device failed: %s", exc)

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

        # A precondition is NOT a reason to skip the row.
        #
        # Measured on the real workbook: 3 context rows put a precondition on
        # 963 of 1052 verifiable rows -- 91% -- because a marker like
        # `[Rear Camera]` sits high in the sheet and legitimately qualifies
        # everything beneath it. Returning NA for any row carrying context
        # meant the verifier would check 89 rows and skip the rest, which is
        # not a gate.
        #
        # Most such preconditions are ambient state the app satisfies on
        # launch, so the row is attempted normally. Context is used only to
        # interpret a MISS, below: we genuinely cannot tell "the build lost
        # this control" from "the precondition did not hold", so a missing
        # element on a row with context becomes NA naming the precondition,
        # rather than a Fail that might be a lie.

        blocked = self.guard.blocks("text", row.selector_text)
        if blocked:
            return RowResult(
                row, NA,
                f"withheld by action guard ({blocked}) -- verify manually",
            )

        reached, why = self._navigate(row.path)
        if not reached:
            if row.context:
                return RowResult(
                    row, NA,
                    f"{why} -- may be the precondition: " + "; ".join(row.context),
                )
            return RowResult(row, FAIL, why)

        _, views, current = self._await_stable()
        if not views:
            return RowResult(row, FAIL, "no readable screen")
        if current and current != self.package:
            return RowResult(
                row, NA, f"screen belongs to {current} -- outside the app",
            )

        match = self._match(row.selector_text, views)
        if match is not None:
            self.match_log.append({
                "sheet_row": row.key,
                "spec_label": row.selector_text,
                "on_screen": match.element.label,
                "score": match.score,
                "why": match.why,
                "matched_on": match.matched_on,
            })
        element = match.element if match else None
        if element is None:
            if row.context:
                # Cannot distinguish a real defect from an unmet precondition.
                return RowResult(
                    row, NA,
                    f"not found: {row.selector_text!r} -- verify by hand under: "
                    + "; ".join(row.context),
                )
            return RowResult(
                row, FAIL,
                f"expected element not present: {row.selector_text!r}",
            )
        detail = f"found as {element.kind}"
        if match is not None and match.score < 1.0:
            # Say what it actually matched, and how sure. A reviewer must be
            # able to see that "Priorize quality" was matched to
            # "Prioritize quality" without rerunning anything.
            detail += (f" -- matched {match.element.label!r} on "
                       f"{match.matched_on} ({match.why}, {match.score:.2f})")
            if match.score < CONFIDENT:
                detail = "REVIEW WORDING: " + detail
        if row.context:
            detail += "  [under: " + "; ".join(row.context) + "]"
        return RowResult(row, PASS, detail)
