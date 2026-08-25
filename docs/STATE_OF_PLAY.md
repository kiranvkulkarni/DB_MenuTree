# MenuTree — where this stands

Honest status, not a pitch. Written against the Realme (RMX3950, Android 16)
and Samsung S26 FE test devices, `com.oplus.camera` and the Samsung camera.

There are now **two** tools in the repo, answering different questions:

| | tool | question | use it for |
|---|---|---|---|
| **Discovery** | `tools/build_menutree.py` | what is in this build? | finding UI the sheet does not have |
| **Verification** | `tools/verify_menutree.py` | does this build match the sheet? | **the release gate** |

Discovery came first and is the larger body of code. Verification is the one
that can gate a release, for the reason in §3.

---

## 1. Discovery — what it does today

```
tools/build_menutree.py
  +- RunLock                       one run per device, refuses overlaps
  +- ElementTreeWalker             walk the app, one screen at a time
  |    +- enumerate_elements()     every labelled element on a screen -> rows
  |    +- worklist                 (screen, element) -> pending/done/unreachable
  |    +- navigation               already-there > BACK > re-click > relaunch+replay
  |    +- ActionGuard              refuses destructive/outbound controls
  +- outputs, per run, in output/<package>_<YYYYMMDD_HHMMSS_mmm>/
       +- <package>_menutree.xlsx    the deliverable
       +- menutree.csv               same data, diff-friendly
       +- <package>_suite.uvta       one UVTA test per row
       +- menutree_rows.json         full detail + stats
       +- <run>.log
```

The workbook mirrors the expected sheet: one row per element, label in the
column matching its depth, plus `Test Result`, `Defect ID`, `Comments`,
**`Needs Manual Test`**, `Kind`, and the row's **UVTA test case** in the last
cell.

### The coverage figure

```
coverage = done / (done + pending + unreachable)
```

`actionable` excludes elements never meant to be pressed — static text, back
buttons, keypad keys, guard-blocked — so the percentage is not inflated by
rows that were never work. Rows needing a human are marked `NA`, not `NT`, so
they do not depress the automated pass rate.

---

## 2. What actually works

Nine real defects found and fixed, each by instrumenting rather than
reasoning:

| defect | how it showed up |
|---|---|
| BACK used to reach screens *below* the walker | trace: wanted SubSet menu, BACK gave main screen |
| tree rooted in another app | a run rooted itself in the Gallery |
| replay verified by exact state key | 38/41 failures incl. empty paths, impossible navigationally |
| foreign app inside our task | `Task{#247 A=camera} topResumed=gallery3d` |
| labels encode toggle state | `face beautyoff` -> `face beautyon` between runs |
| screen identity broken by the same suffix | same screen scored 0.25 against itself |
| `--clear-between-paths` ignored by navigation relaunches | flag honoured at one call site, not the other |
| device screen slept mid-run | camera swapped its UI for a "Tap to show preview" placeholder |
| identity threshold too strict | 32 of 40 recorded misses scored 0.6-0.7 and *were* the same screen |

The last three moved the numbers measurably on the Realme:

| | rows | identify misses | relaunches |
|---|---|---|---|
| before | 306 | 40 | 57 |
| after | 400 | 4 | 22 |

Best measured run: **465 rows, depth 8, 29 screens, 79 elements traversed**.
Starting point was 102 rows at depth 4.

Also solid: the safety layer — the action guard (with identifier-label
normalisation), keypad/MMI protection, IME exclusion, ANR detection, per-run
output folders, and now the run lock and device release. That layer is what
makes the tool safe to point at a real handset, and I trust it more than I
trust the coverage numbers.

---

## 3. Discovery is not reproducible. Verification is why that stopped mattering

Identical code, identical device, identical app:

| run | rows | screens |
|---|---|---|
| A | 465 | 29 |
| B | 291 | 19 |
| C | 90 | 6 |

For a milestone gate this is disqualifying. A build that "loses" 150 rows
might be a regression, or might be Tuesday.

Three attempts to fix it made things worse and are reverted:

| attempt | result |
|---|---|
| deterministic traversal order | 38 and 28 rows — and still not identical |
| + track current screen instead of inferring | 15 and 16 rows — still not identical |
| cheap re-click before relaunch | 465 -> 84 rows |

The common mistake: each was shipped as a default *before* running the
two-run comparison that would have caught it.

**The reframe that resolves this**: the 1,896-row sheet is hand-authored and
repeatedly verified. It is a *specification*, not a discovery output. So the
job was never "discover the tree" — it is "walk the known tree and assert
each row is present".

| problem | under discovery | under verification |
|---|---|---|
| reproducibility | unsolved blocker | **gone** — same rows walked every run |
| preconditions (`on 3rd entry`) | impossible to infer | **gone** — the sheet states them |
| coverage denominator | grows as you explore | **fixed** — 1,896 rows, known up front |
| navigation failure | silent lost coverage | a **Fail on a named row** |

Discovery keeps one narrow job: **finding rows the build has that the sheet
does not**, so the specification can be updated when the app gains UI. That
is a report, not a gate.

---

## 4. Verification — what exists now

```
tools/verify_menutree.py --spec MenuTree.xlsx --package <pkg> --serial <serial>
```

- `spec_reader.py` reads the workbook back into a tree. Depth is positional
  (the `N Depth` columns), so structure is explicit rather than inferred; a
  row's parent is the nearest preceding shallower row.
- `[bracketed]` rows are **context**, not clicks. A marker applies to its
  siblings as well as its descendants, because `[When location Permission is
  OFF in Dut]` sits at the *same* depth as the Cancel / Turn on rows it
  qualifies.
- Depth 1 is the application itself and is never a click step. Left in, every
  path began with a "Camera" tap that matches nothing, and every row failed.
- The verifier tracks position **by index, not by label** — the sheet repeats
  `ON`, `OK` and `Back key icon` at many depths, so `index(label)` resolves to
  the wrong one and corrupts every subsequent row's shared-prefix calculation.
- Results are written to a **copy** of the workbook. The original is never
  modified.

**Status: built and exercised against a fixture, not against the real sheet.**
The real workbook cannot leave your infrastructure, so `tests/spec_fixture.xlsx`
reproduces its shape instead — summary block, header row, depth columns to 7,
bracketed context rows, `[Title]` / `(On/Off)` annotations, a permission
dialog with both branches beneath it.

The next step is yours, and it needs no device:

```
python tools/verify_menutree.py --spec <your workbook> --package <pkg> --dry-run
```

That writes `spec_parsed.json` with every row's reconstructed `path` and
`selector_text`. **Check the paths against the real UI before trusting a
run.** If the paths are right, the walk is straightforward; if they are
wrong, everything downstream is wrong in the same way.

---

## 5. Hard limits, not bugs

**Permission branches cannot be recovered on this device.** Once Android
records a decision, the other branches are never re-offered. Verified:

- `pm clear` does **not** revoke this app's permissions — they are
  `GRANTED_BY_DEFAULT` on a preinstalled OEM app.
- `pm revoke` and `pm reset-permissions` both fail:
  `SecurityException: Neither user 2000 nor current process has
  REVOKE_RUNTIME_PERMISSIONS`.

So `Only this time` / `Don't allow` are permanent manual items here, flagged
as such in the workbook.

**Specification knowledge is unreachable by crawling.** Rows like `on 3rd
entry of camera` encode preconditions no crawler can observe. Under
verification these become `NA` with the precondition quoted, which is the
correct outcome — a human runs them.

**Coverage % is a poor headline metric while discovery is incomplete.** One
run scored 27.6% and another 36.3% — the *lower* one had traversed more
elements (79 vs 57). Finding more of the app grows the denominator. Report
`done`, `pending`, `unreachable` and `needs manual` separately. This limit
does not apply to verification, where the denominator is fixed.

---

## 6. Off-the-shelf alternatives: evaluated, both unusable

Fully written up in [TOOL_EVALUATION.md](TOOL_EVALUATION.md).

- **Google App Crawler — discontinued.** Every documentation URL 404s and
  there is no `crawler` artifact anywhere in Google's Maven index. Nothing to
  download and nothing maintained.
- **Firebase Test Lab Robo — alive, but cannot reach our app.** Cloud-only,
  requires an uploaded APK, and short timeouts. A preinstalled *system*
  camera signed with the platform key cannot be reproduced as an ordinary
  install on a Test Lab device. Worth remembering for any normal app that
  ships as an installable APK.

Worth stealing from Robo: its **scripts** format, a documented JSON encoding
of "get into this state first" — the same shape as the precondition problem.

---

## 7. Running it

Verification (the gate):

```
python tools/verify_menutree.py --spec MenuTree.xlsx --package <pkg> --serial <serial>
```

Exit code 2 if any row fails. Start with `--dry-run` to check the parse.

Discovery (finding what the sheet lacks):

```
python tools/build_menutree.py --package <pkg> --serial <serial> \
    --time-budget 1500 --max-depth 12 --clear-between-paths
```

- Budget 25 min; longer has not helped, because runs end on worklist
  exhaustion rather than time.
- `--clear-between-paths` on a disposable device only.
- Read `done` / `pending` / `unreachable` / `needs manual`, not the single
  percentage.
- Two consecutive runs before trusting any delta.

Both tools take a **run lock** on the device. A second run against the same
serial is refused with the PID of the run that holds it; `--force-lock`
overrides. Both release the device on exit — dropping the stay-awake hold,
force-stopping the app and going Home — from a `finally`, so an interrupted
run does not leave the handset lit and the app open.

---

## 8. What I would do next

1. **Validate the spec reader against the real workbook** (`--dry-run`, no
   device). Everything in §4 depends on the reconstructed paths being right.
2. **First full verification run**, and triage the Fails: each is either a
   real defect, a wording drift between sheet and build, or a navigation gap
   in the verifier. The three need different fixes and only a run separates
   them.
3. **Discovery-vs-spec diff** — rows the build has that the sheet does not.
   This is discovery's remaining job and it does not need reproducibility.
4. Event-level two-run diff for discovery, if reproducibility is ever wanted
   for its own sake: log `(screen, element, outcome)` and find where two runs
   first diverge. That is evidence rather than hypothesis, and it is what
   found every real defect in §2.
