# MenuTree — where this stands, and what to look at next

Written after a long debugging session on the Realme (RMX3950, Android 16)
test device against `com.oplus.camera`. Honest status, not a pitch.

---

## 1. What the tool does today

```
tools/build_menutree.py
  └─ ElementTreeWalker            walk the app, one screen at a time
       ├─ enumerate_elements()    every labelled element on a screen -> rows
       ├─ worklist                (screen, element) -> pending/done/unreachable
       ├─ navigation              already-there > BACK > re-click > relaunch+replay
       └─ ActionGuard             refuses destructive/outbound controls
  └─ outputs, per run, in output/<package>_<YYYYMMDD_HHMMSS_mmm>/
       ├─ <package>_menutree.xlsx    the deliverable
       ├─ menutree.csv               same data, diff-friendly
       ├─ <package>_suite.uvta       one UVTA test per row
       ├─ menutree_rows.json         full detail + stats
       └─ <run>.log
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
rows that were never work. Rows needing a human are marked `NA`, not `NT`,
so they do not depress the automated pass rate.

---

## 2. What actually works

Six real defects were found and fixed, each by instrumenting rather than
reasoning:

| defect | how it showed up |
|---|---|
| BACK used to reach screens *below* the walker | trace: wanted SubSet menu, BACK gave main screen |
| tree rooted in another app | a run rooted itself in the Gallery |
| replay verified by exact state key | 38/41 failures incl. empty paths, impossible navigationally |
| foreign app inside our task | `Task{#247 A=camera} topResumed=gallery3d` |
| labels encode toggle state | `face beautyoff` → `face beautyon` between runs |
| screen identity broken by the same suffix | same screen scored 0.25 against itself |

Best measured run: **465 rows, depth 8, 29 screens, 79 elements traversed**.
Starting point was 102 rows at depth 4.

Also solid: the safety layer. The action guard (with identifier-label
normalisation), keypad/MMI protection, IME exclusion, ANR detection, and
per-run output folders. That layer is what makes the tool safe to point at a
real handset, and I trust it more than I trust the coverage numbers.

---

## 3. What does not work: reproducibility

**This is the blocker, and it is unsolved.**

Identical code, identical device, identical app:

| run | rows | screens |
|---|---|---|
| A | 465 | 29 |
| B | 291 | 19 |
| C | 90 | 6 |

For a milestone gate this is disqualifying. A build that "loses" 150 rows
might be a regression, or might be Tuesday. Until two runs of the same build
agree, a coverage delta cannot be attributed to the app.

Three attempts to fix it all made things worse and are reverted:

| attempt | result |
|---|---|
| deterministic traversal order | 38 and 28 rows — and still not identical |
| + track current screen instead of inferring | 15 and 16 rows — still not identical |
| cheap re-click before relaunch | 465 → 84 rows |

The common mistake: each was shipped as a default *before* running the
two-run comparison that would have caught it. That comparison costs 30
minutes.

### What I would try next

Diff two runs at the event level — log the exact sequence of
`(screen, element, outcome)` and compare where they first diverge. That is
evidence rather than hypothesis, and it is what found all six real defects.
I have not done it yet.

---

## 4. Hard limits, not bugs

**Permission branches cannot be recovered on this device.** Once Android
records a decision, the other branches are never re-offered. Verified:

- `pm clear` does **not** revoke this app's permissions — they are
  `GRANTED_BY_DEFAULT` on a preinstalled OEM app.
- `pm revoke` and `pm reset-permissions` both fail:
  `SecurityException: Neither user 2000 nor current process has
  REVOKE_RUNTIME_PERMISSIONS`.

So `Only this time` / `Don't allow` are permanent manual items here. They are
flagged as such in the workbook.

**Specification knowledge is unreachable by crawling.** Rows in the reference
sheet like `on 3rd entry of camera` or `[When location Permission is OFF in
DUT]` encode preconditions no crawler can observe.

**Coverage % is a poor headline metric while discovery is incomplete.** One
run scored 27.6% and another 36.3% — the *lower* one had traversed more
elements (79 vs 57). Finding more of the app grows the denominator. Report
`done`, `pending`, `unreachable` and `needs manual` separately.

---

## 5. What I want you to look at

Ranked. The first two may make much of section 3 irrelevant.

### 5.1 Google's App Crawler (highest priority)

Official Jetpack tool, runs locally:

```
java -jar crawl_launcher.jar --apk-file app.apk --android-sdk <sdk>
```

It "terminates automatically when there are no more unique actions to
perform" — which is exactly the worklist-exhaustion semantics I hand-built,
except maintained by Google and presumably hardened against the class of
problems that has been costing us days.

**Questions to answer:** does it emit a per-element tree (not just a screen
graph)? Can it reach depth ~18? Is its traversal reproducible run to run?
Does it handle a vendor camera's task/permission behaviour better?

- [App Crawler — Android Developers](https://developer.android.google.cn/studio/test/other-testing-tools/app-crawler)
- [How to run Android App Crawler](https://medium.com/@denysiakimov/how-to-run-android-app-crawler-testing-tool-a0d6f387e89e)

### 5.2 Firebase Test Lab Robo

Produces a **crawl graph** — screens as nodes, actions as edges — plus
`actions.json`, screenshots, video, and stats for actions performed,
activities covered and screens visited. Robo **scripts** (JSON) let you
pre-script a path and then let it explore, which maps directly onto the
precondition problem in §4.

Caveat: it is cloud-based, so a vendor camera on your own hardware may not be
testable there. Worth checking whether the Robo *scripts* format can drive a
local crawl.

- [Robo test (Android)](https://firebase.google.com/docs/test-lab/android/robo-ux-test)
- [Robo scripts reference](https://firebase.google.com/docs/test-lab/android/robo-scripts-reference)
- [Running Robo scripts locally](https://medium.com/android-news/test-robo-scripts-locally-useful-for-firebase-test-lab-pre-launch-reports-41da83d5769f)

### 5.3 Others, lower priority

- [Fastbot (ByteDance)](https://github.com/bytedance/Fastbot_Android) —
  model-based, ML/RL guided, strong at stability. Stochastic, so likely worse
  for reproducibility, but its state-modelling may be worth reading.
- [AppCrawler (Appium-based)](https://github.com/seveniruby/AppCrawler) —
  config-driven traversal with explicit element allow/deny lists, close to
  what we need.
- [Groundhog](https://ics.uci.edu/~seal/projects/groundhog) — accessibility
  crawler; its screen-equivalence handling is relevant to our identity bug.
- [ai-mobile-ui-crawler](https://github.com/ganainy/ai-mobile-ui-crawler) —
  LLM-guided exploration, if the precondition problem needs semantics.

### 5.4 The question only you can answer

**Do other OEM teams solve this by crawling at all?** A 1,896-row sheet with
`on 3rd entry of camera` in it looks hand-authored and then *verified*
repeatedly, not discovered. If that is how it is actually produced, then
**verification against a known tree is the right tool** — walk the expected
rows, assert each is present, report Pass/Fail. That sidesteps discovery,
preconditions, and reproducibility in one move, because you always walk the
same path.

I have raised this twice; it remains the single change most likely to make
this deliverable trustworthy.

---

## 6. If you keep this tool

Run it as:

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
