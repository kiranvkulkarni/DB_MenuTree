"""Export the element tree in the workbook's depth-column layout.

Mirrors the expected sheet: one row per element, the label placed in the
column matching its depth, with Test Result / Defect ID / Comments columns
alongside. Written as CSV so it opens directly in Excel and can be pasted
into the existing workbook without a hard dependency on openpyxl.

Test Result is left as `NT` (not tested). This tool discovers and records the
tree; it does not judge pass or fail. Marking every discovered row `Pass`
would be asserting something never checked.
"""
import csv
import logging
from pathlib import Path
from typing import Dict, List, Sequence

logger = logging.getLogger(__name__)

RESULT_NOT_TESTED = "NT"


def write_csv(
    rows: Sequence[Dict],
    path: Path,
    max_depth: int = 0,
    result_default: str = RESULT_NOT_TESTED,
) -> Path:
    """Write rows in `<depth-1> ... <depth-N>` column layout."""
    if not rows:
        raise ValueError("refusing to write an empty MenuTree sheet")

    depth = max_depth or max(int(r.get("depth", 1)) for r in rows)
    header = ["Test Result", "Defect ID", "Comments"]
    header += [f"{i} Depth" for i in range(1, depth + 1)]

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            cells = [""] * depth
            index = max(1, min(int(row.get("depth", 1)), depth)) - 1
            cells[index] = row.get("label", "")

            comment = row.get("note", "") or ""
            if row.get("blocked"):
                comment = (
                    f"blocked by action guard ({row['blocked']}); "
                    "NOT explored"
                ).strip()
            writer.writerow([result_default, "", comment, *cells])

    logger.info("MenuTree sheet written: %s (%d rows, %d depth columns)",
                path.resolve(), len(rows), depth)
    return path


def summarise(rows: Sequence[Dict]) -> str:
    by_depth: Dict[int, int] = {}
    kinds: Dict[str, int] = {}
    blocked = 0
    for row in rows:
        d = int(row.get("depth", 1))
        by_depth[d] = by_depth.get(d, 0) + 1
        k = row.get("kind", "?")
        kinds[k] = kinds.get(k, 0) + 1
        if row.get("blocked"):
            blocked += 1

    lines = [
        "",
        "=" * 60,
        "  MENUTREE ROWS",
        "=" * 60,
        f"  Total rows      : {len(rows)}",
        f"  Max depth       : {max(by_depth) if by_depth else 0}",
        f"  Blocked (guard) : {blocked}",
        "-" * 60,
        "  rows by depth:",
    ]
    for d in sorted(by_depth):
        lines.append(f"    depth {d:<3} {by_depth[d]}")
    lines.append("-" * 60)
    lines.append("  rows by kind:")
    for k in sorted(kinds, key=lambda x: -kinds[x]):
        lines.append(f"    {k:<10} {kinds[k]}")
    lines.append("=" * 60)
    return "\n".join(lines)
