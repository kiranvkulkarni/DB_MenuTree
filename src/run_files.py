"""Names of the files a run writes -- one place, unique per run.

Every run has its own folder under output/ (`<package>_<YYYYMMDD_HHMMSS_mmm>`,
`branch_<stamp>`, `verify_<package>_<stamp>`), and every file in it starts
with the folder's name. Two MenuTrees from two runs could not be open in
Excel at once while both were called `<package>_menutree.xlsx`.

Readers go through `find`, which also accepts the names runs used before
2026-10-20, so older run folders still read.
"""
from pathlib import Path
from typing import Optional

SUFFIX = {
    "rows": "rows.json",               # every row, full detail
    "walked": "rows.walked.json",      # the rows as walked, kept on re-export
    "csv": "menutree.csv",
    "xlsx": "menutree.xlsx",
    "uvta": "suite.uvta",
    "repository": "object_repository.json",
    "pages": "page_objects.py",
    "branch": "branch.json",
    "guide": "REVIEW_GUIDE.md",
    "info": "run_info.json",          # phone, software, build, app version
}

LEGACY = {
    "rows": ["menutree_rows.json"],
    "walked": ["menutree_rows.walked.json"],
    "csv": ["menutree.csv"],
    "xlsx": ["*_menutree.xlsx"],
    "uvta": ["*_suite.uvta"],
    "branch": ["branch.json"],
    "guide": ["REVIEW_GUIDE.md"],
}


def run_file(folder, kind: str) -> Path:
    """Where a run writes a file of this kind: `<folder name>_<suffix>`."""
    folder = Path(folder)
    return folder / f"{folder.name}_{SUFFIX[kind]}"


def find(folder, kind: str) -> Optional[Path]:
    """The file of this kind in a run folder, under its current or old name."""
    folder = Path(folder)
    current = run_file(folder, kind)
    if current.exists():
        return current
    for pattern in LEGACY.get(kind, []):
        hits = sorted(folder.glob(pattern))
        if hits:
            return hits[0]
    return None
