# MenuTree generator — instructions for Claude

This repo walks an Android app's UI on a connected phone and writes an Excel
**MenuTree** (one row per menu item, depth columns, pre-order) plus a UVTA test
suite. The reference app is the Samsung camera (`com.sec.android.app.camera`)
on a Galaxy S25 Ultra; the target is the device team's hand-authored MenuTree.
The tool is release-gating.

**Read [docs/HANDOFF.md](docs/HANDOFF.md) first.** It has the current state,
open items and the next steps. [docs/HANDLING.md](docs/HANDLING.md) is the
agreed policy for every kind of element. Change it only with the user's
agreement, and record every agreed rule there.

## Rules that are not negotiable

- **App-agnostic.** No app knowledge in walker code (`src/crawler/*`). The walker
  decides from structure: class, clickable, checkable, selected, enabled, lists,
  bounds. It also decides from observation: press, see what changed, put it
  back. Anything specific to one app is data in `profiles/<package>.json`, one
  file per package, plus `aliases/`. An app with no profile is walked on
  structure alone. `tests/test_recursive_walk.py` fails if app words appear in
  the walker. If only English wording separates two cases, find a structural
  signal or ask the user.
- **Start every exploration from a cleared app** (clear data and cache). That
  way first-launch and permission pop-ups get recorded. `build_menutree.py` and
  `walk_branch.py` do this unless `--no-reset` is given.
- **Never change a setting.** Options are listed, not selected. Sliders are
  never swiped or tapped. Switches and relabelling controls are put back. The
  action guard refuses destructive, outbound and commerce controls. Keypad keys
  are never pressed.
- **Company material is never committed.** That covers `S25_Ultra_MenuTree/`,
  any `*_MenuTree/` folder and `MenuTree*.xlsx`; all are gitignored. The user
  cannot share company workbooks off company machines. See HANDOFF.md, "Open
  decisions", about `S25U_MenuTree.xlsx`.
- **Selector priority:** text, then description, then resource id, then xpath.

## How to work with this user

- **Screen by screen, never "run and read the wreckage".** For each screen:
  1. Inspect it on the phone (`tools/inspect_screen.py`, or a uiautomator2 dump).
  2. Probe what each element does.
  3. Propose the handling and agree it with the user (ask; give a recommendation).
  4. Implement it with a test that uses a real dump as a fixture.
  5. Re-walk only that branch (`tools/walk_branch.py`) and compare.

  After a change to shared code, re-walk earlier branches to catch
  regressions. Do not start a full run (2.5 h) to test a fix; ask first.
- **Reviews:** the user copies the generated `MenuTree` sheet to a sheet named
  `MenuTree_To_Be` and writes comments in column R, "Review Comments"
  (New / Move / Delete plus text). Each comment is a rule for the **whole
  tree**, every mode and screen, not one row. Confirm ambiguous ones, then
  record the agreed rules in HANDLING.md.
- **Menus differ by mode.** Quick controls and Settings change per camera mode.
  For example, PORTRAIT has no Resolution, and NIGHT has no Flash or Filters.
  A row missing in one mode is not a bug. Never hard-code expected items.
- **After each full run:** hand over the workbook with a short `REVIEW_GUIDE.md`
  in the run folder. Put known gaps first, then where to check each applied
  rule, then the comments legend.
- **Report outcomes plainly.** If a walk pressed something it shouldn't have,
  or left a setting changed, say so.

## Commands

```bash
# setup (Python 3.11, Windows; adb on PATH; USB debugging on)
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
adb devices                                   # the serial for --serial

# tests (no device needed) -- run all before every commit
for t in tests/test_*.py; do .venv/Scripts/python.exe -B $t >/dev/null || echo FAIL $t; done

# one screen: what is on it and how the walk would treat each element
.venv/Scripts/python.exe -B tools/inspect_screen.py --package com.sec.android.app.camera \
    --serial <serial> --path "PORTRAIT,Quick controls,Go to Settings" [--probe] [--no-reset]

# one branch (minutes): rows printed and saved to output/branch_<stamp>/branch.json
.venv/Scripts/python.exe -B tools/walk_branch.py --package com.sec.android.app.camera \
    --serial <serial> --path "MORE,PRO" [--only "A,B"] [--skip "Quick controls"]

# full run (about 2.5 h): output/<package>_<stamp>/ -> xlsx, csv, rows json, uvta, log
.venv/Scripts/python.exe -B tools/build_menutree.py --package com.sec.android.app.camera \
    --serial <serial> --revisits top --time-budget 28800

# keep the dump each screen was listed from (to see what the walk saw)
MENUTREE_SAVE_DUMPS=<folder> .venv/Scripts/python.exe -B tools/walk_branch.py ...

# re-apply the layout rules to an existing run without walking again
.venv/Scripts/python.exe -B tools/build_menutree.py --package com.sec.android.app.camera \
    --skip-walk --output-dir output/<run folder>
```

## Where things are

| Path | What |
|---|---|
| `src/crawler/recursive_walk.py` | The walker: pre-order DFS, panels vs screens, switches, selectors, returns, replay, separate passes, first launches, review rules |
| `src/crawler/element_tree.py` | Base walker: sweeping a screen (scrolling), tapping, `TreeNode` (a sheet row), writing the rows json |
| `src/crawler/elements.py`, `hierarchy.py` | Elements from the XML dump; dialogs, scroll containers, sliders, bounds |
| `src/generator/` | Workbook, csv and UVTA writers |
| `profiles/<package>.json` | Per-app data: do_not_press, walk_separately, leaves_app, list_only, first_launches, launch_answers, suggestions, aliases |
| `tools/` | build_menutree, walk_branch, inspect_screen, compare_tobe, verify_menutree |
| `tests/` | Offline suites; `tests/fixtures/*.xml` are real screen dumps |
| `docs/` | HANDOFF (start here), HANDLING (policy), ARCHITECTURE, METHOD; STATE_OF_PLAY is August history |
