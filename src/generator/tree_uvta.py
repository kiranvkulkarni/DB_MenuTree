"""Turn MenuTree rows into UVTA test cases.

One test case per row: navigate the row's path, then assert the element is
present. That mirrors the workbook, where each row carries its own
Pass/Fail — the test *is* "is this item still there, reachable by this
route".

Deterministic, like the graph-based emitter: same rows in, byte-identical
suite out. Syntax comes from `uvta_syntax` so there is still exactly one
place that knows what UVTA looks like.
"""
import logging
import re
from typing import Dict, List, Sequence, Tuple

from ..parser.menu_tree import Selector
from . import uvta_syntax as uvta
from .path_emitter import TestCase

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9]+")

# Sheet annotations appended to a label for human readers. They are not part
# of the on-screen text, so they must never reach a selector.
_ANNOTATION = re.compile(
    r"\s*(\[Title\]|\[subtitle\]|\(On/Off\)|\(Radio button On/Off\))\s*$"
)


def _raw(row: Dict) -> str:
    """On-screen text for the row, usable as a selector.

    Prefers the recorded raw label; falls back to stripping the annotation
    off the display label so rows captured before `raw_label` existed still
    produce valid selectors.
    """
    raw = (row.get("raw_label") or "").strip()
    if raw:
        return raw
    return _ANNOTATION.sub("", row.get("label", "")).strip()

# Rows that describe navigation furniture rather than a feature.
_SKIP_KINDS = {"root", "back"}


def _selector_for(row: Dict) -> Selector:
    """How a test should address this row, resolved when it was enumerated.

    The order is text -> description -> resource id -> xpath, and which of
    those a control offers is entirely up to whoever built it. Reading it off
    the XML dump per element is the only way to know.

    Emitting `text` for everything was wrong and silently so: a row's label is
    whichever of text or content-desc happened to be non-empty, so every icon
    -- "Flash icon", "Back key icon", anything with no visible text -- got a
    `text "Flash"` selector that cannot match at runtime. The suite looked
    complete and would have failed on execution.
    """
    kind = row.get("selector_kind")
    value = row.get("selector_value")
    if kind and value:
        return Selector(kind, str(value))
    # Older rows files predate selector resolution; text is the best guess.
    return Selector("text", _raw(row))


def _name_for(row: Dict, used: Dict[str, int]) -> str:
    parts = ["Verify", *row.get("path", []), _raw(row)]
    base = _SAFE.sub("_", "_".join(p for p in parts if p)).strip("_")[:110]

    # Suffix until genuinely unique. A plain counter is not enough: a screen
    # holding "Keypad" and "Keypad 2" makes the disambiguated name of the
    # first collide with the natural name of the second.
    candidate, n = base, 1
    while candidate in used:
        n += 1
        candidate = f"{base}__{n}"
    used[candidate] = 1
    return candidate


def emit_indexed(
    rows: Sequence[Dict],
    package: str,
    include_blocked: bool = False,
) -> Tuple[List[TestCase], Dict[int, str]]:
    """Emit cases, plus a row-index -> rendered-case map.

    The workbook puts each row's test in its own last cell, so the mapping
    has to survive the rows that produce no case (the root, back buttons,
    and anything the guard blocked).
    """
    cases: List[TestCase] = []
    by_row: Dict[int, str] = {}
    used: Dict[str, int] = {}
    skipped_blocked = 0

    for row_index, row in enumerate(rows):
        if row.get("kind") in _SKIP_KINDS:
            continue
        if row.get("blocked") and not include_blocked:
            # The guard refused to press it, so the route was never walked and
            # a test asserting it would be untested guesswork.
            skipped_blocked += 1
            continue

        label = _raw(row)
        if not label:
            continue

        # Each verify proves the CLICK BEFORE IT landed, by asserting the
        # thing that click was supposed to reveal.
        #
        # This used to click a step and then assert that same step still
        # existed, which proves nothing either way: a menu item that opens a
        # submenu is usually replaced by it, so the assertion passed
        # vacuously when the item happened to stay on screen and failed
        # spuriously when it did not. Neither outcome said whether the click
        # worked.
        #
        # Chaining it -- click A, verify B; click B, verify C -- means every
        # step is checked by the step after it, and the final assertion is
        # the row itself. A test that passes has demonstrably navigated the
        # whole path.
        path = list(row.get("path", []))
        path_selectors = [
            Selector(k, v) for k, v in (row.get("path_selectors") or [])
        ]
        # Older rows files carry labels only; text is the best guess there.
        if len(path_selectors) != len(path):
            path_selectors = [Selector("text", a) for a in path]

        targets = path_selectors + [_selector_for(row)]

        steps = [uvta.launch(package)]
        for index, step_selector in enumerate(path_selectors):
            steps.append(uvta.click(step_selector))
            steps.append(uvta.verify_exists(targets[index + 1]))
        if not path_selectors:
            # Nothing to click: the row is on the entry screen.
            steps.append(uvta.verify_exists(_selector_for(row)))

        case = TestCase(
            name=_name_for(row, used),
            steps=steps,
            target_state="",
            target_activity=package,
            depth=int(row.get("depth", 1)),
        )
        cases.append(case)
        by_row[row_index] = case.render()

    cases.sort(key=lambda c: (c.depth, c.name))
    if skipped_blocked:
        logger.info(
            "Skipped %d guard-blocked row(s); those routes were never walked.",
            skipped_blocked,
        )
    logger.info("Emitted %d test case(s) from %d MenuTree row(s).",
                len(cases), len(rows))
    return cases, by_row


def emit(
    rows: Sequence[Dict],
    package: str,
    include_blocked: bool = False,
) -> List[TestCase]:
    return emit_indexed(rows, package, include_blocked)[0]
