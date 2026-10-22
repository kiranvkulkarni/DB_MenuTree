# MenuTree AutoQA Agent

Tooling for the **MenuTree** release gate. A MenuTree is an element-by-element,
depth-ordered inventory of an Android app. It is exported as the expected
workbook, with a UVTA test case per row.

```bash
# DISCOVERY -- walk the app on the phone and write its MenuTree (the main tool)
python tools/build_menutree.py --package <pkg> --serial <serial> --revisits top --time-budget 28800

# VERIFICATION -- walk a hand-authored sheet and assert every row is present
python tools/verify_menutree.py --spec MenuTree.xlsx --package <pkg> --serial <serial>
```

Discovery is the tool in daily use. It walks the app screen by screen from a
cleared start, pressing what is safe to press and putting back anything it
changes. The workbook it writes is reviewed by the sheet's owner, and the
review comments become rules for the whole tree. On the Galaxy S25 Ultra:

| App | Package | Full run | Rows |
|---|---|---|---|
| Samsung camera | `com.sec.android.app.camera` | about 2 h | about 3,700 |
| Samsung Internet | `com.sec.android.app.sbrowser` | about 2 h | about 2,100 |

Each run also writes an **object repository**: every UI element with all its
locators, as JSON, a workbook sheet and Python page objects (§5). Long press
is walked too, wherever a control takes one.

Verification walks the rows of an existing sheet instead. Its denominator is
fixed by the sheet, so it suits a pass/fail gate.

**Continuing this work? Start with [CLAUDE.md](CLAUDE.md) and
[docs/HANDOFF.md](docs/HANDOFF.md).**

---

## 0. Documentation

| Document | Read it when |
|---|---|
| **[CLAUDE.md](CLAUDE.md)** | First. The rules that are not negotiable (app-agnostic, cleared start, never change a setting, no company material in git), how the work is done (screen by screen, reviews), and the commands. Claude Code reads it automatically. |
| **[docs/HANDOFF.md](docs/HANDOFF.md)** | Second. Where things stand, the decisions agreed with the sheet's owner, open items, known gaps, and the latest full run. |
| **[docs/HANDLING.md](docs/HANDLING.md)** | Before changing how any element is treated. The agreed policy for every kind of element: switches, options, panels, dials, captions, pop-ups, the review rules. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Before changing the structure. Why the system is shaped this way and the traps it avoids. |
| [docs/METHOD.md](docs/METHOD.md) | How this was built; what each guardrail is scar tissue from. |
| [docs/MODULES.md](docs/MODULES.md) | "Where do I go to change X?" |
| [docs/STATE_OF_PLAY.md](docs/STATE_OF_PLAY.md), [docs/TOOL_EVALUATION.md](docs/TOOL_EVALUATION.md) | History (August 2026): the earlier worklist walker, and why existing crawlers could not serve this deliverable. |
| This file | Setup, running, reading the output. |

## 1. Design principle: nothing infers the structure

Test *steps* are derived from the walk itself, never inferred afterwards. The
same tree always produces a byte-identical `.uvta` file, so a difference
between two runs is attributable to the app rather than to variance in a
model.

An earlier version of this project flattened the crawl into a linear click
stream and asked an LLM to infer which clicks belonged to which branch. That
cannot work even in principle: a flat stream has no parent pointers, so the
model is being asked to recover information that was already discarded. The
structure now comes from the traversal, which knows it.

**There is no LLM anywhere in this codebase**, and no LLM SDK is installed.

## 2. Selectors come from the XML dump

Which handle a control offers is up to whoever built it — some expose text,
some only a content-description, some only a resource id, some nothing at
all. Every element is resolved from the dump in the confirmed order:

```
1. text        2. description        3. resource id        4. xpath
```

xpath is structural and always available, so it is the final fallback rather
than one of the priority keys. `src/parser/selectors.py` owns this and is the
only place it is decided.

This matters in the emitted suite: a row's label is whichever of text or
content-desc was non-empty, so addressing every row by `text` produces
selectors that cannot match for any icon. The resolved selector is carried
per row from enumeration through to the `.uvta`.

## 3. Setup

Requirements: Python 3.11 (tested; 3.10+ should work), Android SDK
`platform-tools` (adb) on PATH, and a phone with USB debugging enabled,
unlocked, ideally with "Stay awake" on while charging.

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows
# source .venv/bin/activate          # Linux / macOS

pip install --upgrade pip
pip install -r requirements.txt
```

That is the whole setup. The packages are pinned to the versions last
tested. There is no APK handling, no patching, and no external crawler. The
first uiautomator2 call installs its agent on the phone.

Find your device with `adb devices` and pass it as `--serial`.

## 4. Running

### Discovery (the MenuTree of an app)

```bash
# Full run: about 2 h on the camera. Writes output/<package>_<YYYYMMDD_HHMMSS_mmm>/
python tools/build_menutree.py --package com.sec.android.app.camera \
    --serial <serial> --revisits top --time-budget 28800

# One screen: what is on it and how the walk would treat each element
python tools/inspect_screen.py --package <pkg> --serial <serial> \
    --path "PORTRAIT,Quick controls,Go to Settings" [--probe] [--no-reset]

# One branch (minutes): the way every fix is developed and checked
python tools/walk_branch.py --package <pkg> --serial <serial> \
    --path "MORE,PRO" [--only "A,B"] [--skip "Quick controls"]

# Re-apply the layout rules to an existing run, without walking again
python tools/build_menutree.py --package <pkg> --skip-walk --output-dir output/<run>
```

- **Start state.** Every walk starts by clearing the app's data and cache,
  so first-launch and permission pop-ups are recorded. `--no-reset` skips it.
- **`--revisits`.** This sets how a screen reached again by another route is
  treated (Settings, from every camera mode). `top` (the agreed default)
  lists its rows but opens only rows not seen before; `full` walks it every
  time; `note` writes one row saying where it was listed. A screen counts as
  seen only when it is at least 90% the same as one walked earlier, so the
  camera's rear PHOTO and VIDEO Settings (with their own rows) are walked,
  while the MORE modes' and the front PHOTO and VIDEO Settings are listed
  with "listed; opened under ...". The same rule holds rear and front.
- **App-specific decisions** live in `profiles/<package>.json`: what not to
  press, what to walk as a separate pass (the front camera, Secret mode),
  first-launch answers (including full-screen welcome pages), what to open
  but only list (`list_only`, by label, resource id or "X (long press)"),
  words that mark a choice (`option_words`), and sheet aliases. An app
  without a profile is walked on structure alone. Each profile's `_about`
  says why every entry is there.
- **Long press**: every control the phone marks `long-clickable` also gets a
  "<control> (long press)" row, walked like any press (in a list, the first
  item only).
- **Diagnosis.** `MENUTREE_SAVE_DUMPS=<folder>` saves the screen dump each
  screen was listed from.

### Verification (against a hand-authored sheet)

```bash
# Check the sheet parses correctly first. No device needed.
python tools/verify_menutree.py --spec MenuTree.xlsx --package <pkg> --dry-run

# Then the real run. Writes a COPY of the workbook; the original is untouched.
python tools/verify_menutree.py --spec MenuTree.xlsx --package <pkg> --serial <serial>
```

`verify_menutree.py` exits 2 if any row fails.

Both tools write to a per-run folder under `output/`, so no run ever
overwrites another. Both take a **run lock** on the device: a second run
against the same serial is refused, naming the PID that holds it
(`--force-lock` overrides). Both release the device on exit, from a
`finally`: they drop the stay-awake hold, force-stop the app and go Home, so
an interrupted run still hands the handset back.

> **Safety on a personal device.** The action guard is on by default and
> refuses destructive, outbound, account and commerce controls; keypad keys
> are recorded but never pressed (`*2767*3855#` is a factory reset). Do not
> pass `--no-guard` on a device holding real data.

## 5. Reading the results

**Discovery** writes one folder per run, `output/<package>_<YYYYMMDD_HHMMSS_mmm>/`,
and every file in it starts with the folder's name, so workbooks from two runs
can be open at once:

| File | What |
|---|---|
| `<run>_menutree.xlsx` | Sheets `Summary` (phone, software/build/hardware versions, app version, depth and size), `MenuTree` (the deliverable) and `ObjectRepository` |
| `<run>_menutree.csv` | The same rows, diff-friendly |
| `<run>_rows.json` | Every row in full detail, with all its locators |
| `<run>_suite.uvta` | One UVTA case per row; a `# object: <name>` line before each names its element |
| `<run>_run_info.json` | What the tree was generated on: phone, software, build and hardware versions, app name, version and package; shown as the workbook's first sheet, `Summary`, with the tree's depth and size |
| `<run>_object_repository.json` | The object repository: every UI element under a stable name (`PORTRAIT.QuickControls.Flash`) with all its locators -- text, content-desc, resource-id, class, xpath, UiAutomator -- and the preferred one |
| `<run>_page_objects.py` | The same as page objects: one class per screen, one `Locator` per element |
| `<run>.log` | The walk's log |

Folders written before 2026-10-20 use the older names (`menutree_rows.json`
and so on); the tools still read them.
In the workbook:

- **Depth columns.** Each row's label sits in the column for its depth. The
  cell right after the label holds the item's widget or state, for example
  `Balanced | radio (On/Off)` or `Resolution | BACK_CAMERA_PRO_PICTURE_SIZE_NORMAL`.
- **Comments column.** It says what the walk did with the row, for example
  `option -- listed, not selected`, `state after pressing -- put back`,
  `on a slider -- listed, not pressed`, `listed; opened under ...`, or
  `blocked by action guard`. [docs/HANDLING.md](docs/HANDLING.md) explains
  each one.

The run summary is the first thing to read. A good run has
`returns_failed: 0` and `switches_not_restored: 0`, and no
`could not put ... back` warnings in the log.

**Reviews.** The sheet's owner copies `MenuTree` to a sheet named
`MenuTree_To_Be` and writes comments in column R, "Review Comments". Each
comment is applied as a rule across the whole tree.

**Verification** reports Pass / Fail / NA and, above the pass rate, **how
many rows were actually judged**. Read that first. A percentage over a small
slice of the sheet is not a gate result, and the tool says so out loud when
judged rows fall below 60% — see [METHOD.md](docs/METHOD.md) §3.1 for the run
that reported 100% over 15% of its rows.

`tools/drift_report.py` groups failures by branch and classifies each as
renamed / restructured / absent, which is the list to work from when the
sheet is older than the build.

### Wording the matcher cannot bridge

Some sheet labels share no word with what the app exposes. The sheet says
`12M`; the XML dump says `BACK_CAMERA_PICTURE_SIZE_NORMAL`. That is a
different vocabulary, not wording drift, and no threshold reaches it.

Those are recorded, not guessed:

```bash
python tools/verify_menutree.py --spec MenuTree.xlsx --package <pkg>     --serial <serial> --aliases aliases/samsung_camera.json
```

One label maps to a **list** of targets, because the same option is named
differently per mode (`..._PICTURE_SIZE_NORMAL` in Photo,
`..._PRO_PICTURE_SIZE_NORMAL` in Pro). Only labels that cannot match on their
own belong there — `Flash Auto` already reaches `BACK_FLASH_AUTO` unaided,
and aliasing it would hide a future rename. See
[METHOD.md](docs/METHOD.md) §3.8.

## 5a. Running against a different model

Nothing here is specific to one handset. To take it to another device:

1. `adb devices`, then pass the new `--serial`. Confirm the package name —
   `adb shell pm list packages | grep camera`.
2. **Run verification once with no `--aliases`.** It writes
   `alias_review.json` listing every inexact match it made.
3. Read the failures. A near-zero score against an internal constant is a
   vocabulary gap; a mid-range score is usually a genuine rename, and belongs
   in the drift report rather than in an alias file.
4. Confirm the gaps against a real dump, add them, re-run with `--aliases`.

Expect the constant *families* to carry across Samsung models and their
*members* not to — a model with no 200MP sensor has no `ULTRA_HIGH`. An alias
whose targets are all absent never fires, so a stale entry costs a Fail and
never a false Pass.

---

## 6. Known gaps

The current list, with what is being done about each, is in
[docs/HANDOFF.md](docs/HANDOFF.md) §3 and §5. In short:

1. **Labels that come and go by themselves** are listed as rows. Examples are
   a top-bar divider ("Bar") in video modes, and the aperture readout
   ("F 1.7") for a moment after a change.
2. **Live values** (a dial's level, NIGHT's shutter time) can change between
   being listed and being pressed ("could not be located"). They are listed
   but never pressed.
3. **Text input is not driven.** Fields are enumerated, never typed into.
4. **Permissions.** `pm clear` does not revoke a preinstalled app's
   permissions. The first-launch chain is recorded after a clear, but a
   permission Android already holds is not asked again.

---

## 7. Development

Everything below runs without a device, in seconds. Run it before any device
work and before every commit:

```bash
for t in tests/test_*.py; do python -B $t >/dev/null || echo FAIL $t; done
python tests/make_spec_fixture.py   # a workbook shaped like the real deliverable (generic labels)
python tools/verify_menutree.py --spec tests/spec_fixture.xlsx --package com.example --dry-run
```

`tests/fixtures/*.xml` are real screen dumps. A new handling gets a test
against the dump of the screen it was found on.

The MenuTree sheet's layout: columns A-D width 8, depth 1-14 (E-R) width 16,
depth 15-18 (S-V) width 4, every cell wrapped; the last column, "Object",
names each row's element in the ObjectRepository sheet.

The working method:
1. Inspect the screen on the phone.
2. Probe what each element does.
3. Agree the handling with the sheet's owner.
4. Implement it, with a test.
5. Re-walk only that branch, and compare it with the last full run.

Never test a fix with a full run. Company material, such as the device
team's MenuTree or anything transcribed from it, stays out of git; see
`.gitignore`.

> **Safety:** an exhaustive crawler presses every button it finds. On the
> Phone app it dialled numbers. The action guard is on by default and refuses
> destructive, outbound, account and commerce controls; keypad keys are
> recorded but never pressed, because `*2767*3855#` is a factory reset. Do not
> pass `--no-guard` on a device holding real data.

---

## 8. Troubleshooting

**`Expected exactly one online device`** — pass `--serial`; `adb devices` to
find it.

**`Device 'X' is in state 'unauthorized'`** — toggle USB Debugging off/on and
accept the RSA fingerprint prompt.

**`Package 'X' is not installed`** — verify with `adb shell pm path <package>`.

**`Device <serial> is already being driven by PID N`** — a run is still live.
Two runs interleaving taps corrupt both, invisibly. Stop the named process
(`taskkill /PID N /F`) or pass `--force-lock` if you are certain it is dead.

**The device keeps acting after a run finished** — check for an orphaned run
(`tasklist | findstr python`) and clear the wake-lock:

```bash
adb shell svc power stayon false
adb shell am force-stop <package>
```

**A run reports far fewer rows than expected** — check `output/.run-lock-<serial>`
first. If it exists the run is still going, and what you are reading is a
mid-run checkpoint rather than a result.

**The log turns into nothing but `unreachable` warnings** — a modal is
blocking the app, most often a first-run prompt like "Turn on Location
tags?". The run is not stuck; it is walking a screen it cannot touch and
recording healthy controls as missing. Both tools now decline such a prompt
themselves (`blocking_dialogs_cleared` in the summary counts it). To clear
one by hand mid-run, without killing the run:

```bash
adb -s <serial> shell input keyevent KEYCODE_BACK
```

Three consecutive write-offs prompt a **look** — cheap, and a bottom sheet or
a consent page does not look like a dialog while blocking just as completely.
But the recovery acts only on positive evidence: the foreground package is
not ours, a modal is recognised, or a non-committal button is genuinely on
screen. Otherwise it does nothing, because a streak of failures is usually
the screen-identity problem (METHOD.md §6), and pressing BACK on a hunch
exits the app. What it did write off while genuinely blocked is put back.

Read `blocking_dialogs_cleared`, `blocked_recoveries` and `elements_requeued`
before believing a low coverage figure. They are what tells an app that is
small apart from an app that was behind a pop-up.

**A full run stops early** — the default `--time-budget` is 3600 s. A full
camera run needs about 2 h, so pass `--time-budget 28800`.

**The phone dozed or disconnected mid-run** — reconnect it and unlock it. The
walker holds the screen awake while it runs. If a run was interrupted, its
rows so far are in `<run>_rows.json` in the run folder.

**A row is missing that is on the screen** — re-walk that branch with
`MENUTREE_SAVE_DUMPS=<folder>` and compare the saved dump with what was listed.

**Low coverage (worklist walker only)** — read `phase_seconds` in
`<run>_rows.json`. Runs end on the time budget, so the largest phase is the
biggest coverage lever.
