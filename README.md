# MenuTree AutoQA Agent

Tooling for the **MenuTree** release gate: an element-by-element,
depth-ordered inventory of an Android app, exported as the expected workbook
with a UVTA test case per row.

Two tools, answering different questions:

```bash
# THE GATE — walk the hand-authored sheet, assert every row is present
python tools/verify_menutree.py --spec MenuTree.xlsx --package <pkg> --serial <serial>

# DISCOVERY — find UI the sheet does not have yet
python tools/build_menutree.py --package <pkg> --serial <serial> --time-budget 1500
```

Verification is what can gate a release: the same rows are walked every run,
the denominator is fixed by the sheet, and a row that cannot be reached is a
named Fail rather than silently missing coverage. Discovery is not
reproducible run to run and should not be used as a gate — see
[docs/STATE_OF_PLAY.md](docs/STATE_OF_PLAY.md) §3.

---

## 0. Documentation

| Document | Read it when |
|---|---|
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Before changing anything. Explains *why* the system is shaped this way, what was tried and rejected, and the traps that are easy to fall back into. |
| **[docs/STATE_OF_PLAY.md](docs/STATE_OF_PLAY.md)** | **Read this first.** Honest status: what works, what does not, the unsolved reproducibility problem, hard platform limits, and the existing tools worth evaluating before investing further. |
| **[docs/TOOL_EVALUATION.md](docs/TOOL_EVALUATION.md)** | Why Google App Crawler (discontinued) and Firebase Robo (cloud-only, APK-upload) cannot serve this deliverable — and why verifying a known tree beats discovering one. |
| **[docs/METHOD.md](docs/METHOD.md)** | **How this was built and how to extend it.** The method, the mistakes and what each guardrail is scar tissue from. Read before changing matching, thresholds, or anything that turns a Fail into an NA. |
| **[docs/MODULES.md](docs/MODULES.md)** | "Where do I go to change X?" Code map, all four back-ends, device lifecycle rules, common tasks. |
| This file | Setup, running, reading the report. |

Two entry points, sharing everything except how they choose what to visit:

| | tool | question |
|---|---|---|
| Discovery | `tools/build_menutree.py` | what is in this build? |
| **Verification** | `tools/verify_menutree.py` | **does this build match the sheet?** |

Verification is the gate. Discovery finds UI the sheet does not have yet.

---

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

Requirements: Python 3.10+, Android SDK with `platform-tools` on PATH, and a
device with USB debugging enabled.

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows
# source .venv/bin/activate          # Linux / macOS

pip install --upgrade pip
pip install -r requirements.txt
```

That is the whole setup. Four packages, no APK handling, no patching, no
external crawler.

Find your device with `adb devices` and pass it as `--serial`.

## 4. Running

### The MenuTree tools (back-ends C and D)

```bash
# Check the sheet parses correctly first. No device needed.
python tools/verify_menutree.py --spec MenuTree.xlsx --package <pkg> --dry-run

# Then the real run. Writes a COPY of the workbook; the original is untouched.
python tools/verify_menutree.py --spec MenuTree.xlsx --package <pkg> --serial <serial>

# Discovery, to find UI the sheet does not have.
python tools/build_menutree.py --package <pkg> --serial <serial> \
    --time-budget 1500 --max-depth 12
```

Both write to a per-run folder `output/<package>_<YYYYMMDD_HHMMSS_mmm>/`, so
no run ever overwrites another. `verify_menutree.py` exits 2 if any row fails.

Both take a **run lock** on the device: a second run against the same serial
is refused, naming the PID that holds it. `--force-lock` overrides. Both
release the device on exit — dropping the stay-awake hold, force-stopping the
app and going Home — from a `finally`, so an interrupted run still hands the
handset back.

> **Safety on a personal device.** The action guard is on by default and
> refuses destructive, outbound, account and commerce controls; keypad keys
> are recorded but never pressed (`*2767*3855#` is a factory reset). Do not
> pass `--no-guard` on a device holding real data.

## 5. Reading the results

**Discovery** reports `done / (done + pending + unreachable)`. `actionable`
excludes what was never meant to be pressed — static text, back buttons,
keypad keys, guard-blocked controls — so the figure is not inflated by rows
that were never work.

Runs end on the clock, not on exhausting the app: a run with
`still pending > 0` ran out of time, so a cheaper action is directly more
coverage. Read `phase_seconds` before optimising anything.

**Verification** reports Pass / Fail / NA and, above the pass rate, **how
many rows were actually judged**. Read that first. A percentage over a small
slice of the sheet is not a gate result, and the tool says so out loud when
judged rows fall below 60% — see [METHOD.md](docs/METHOD.md) §3.1 for the run
that reported 100% over 15% of its rows.

`tools/drift_report.py` groups failures by branch and classifies each as
renamed / restructured / absent, which is the list to work from when the
sheet is older than the build.

---

## 6. Known gaps

1. **Discovery is not reproducible.** Identical code, device and app produced
   465, 291 and 90 rows. This is why verification, not discovery, is the gate.
2. **Screen identity is the limiter.** Navigation shortcuts land where
   expected about 10% of the time, because the same-looking menu appears under
   many parents with identical element sets. See METHOD.md §6.
3. **Permission branches cannot be replayed.** `pm clear` does not revoke a
   preinstalled app's permissions, and `pm revoke` fails for shell. Once
   Android records a decision, the other branches are never offered again.
4. **Specification knowledge is unreachable by crawling.** Rows like
   `on 3rd entry of camera` encode preconditions no crawler can observe.
5. **Text input is not driven.** Fields are enumerated, never typed into.

---

## 7. Development

Everything runs without a device, in seconds. Run it before any device work:

```bash
python tests/test_hierarchy.py     # XML parsing, state keys, selector priority
python tests/test_matching.py      # sheet wording vs screen text, both directions
python tests/test_navigation.py    # the rise-then-descend plan
python tests/test_run_lock.py      # one run per device
python tests/make_spec_fixture.py  # a workbook shaped like the real deliverable
```

Then, still with no device:

```bash
python tools/verify_menutree.py --spec tests/spec_fixture.xlsx     --package com.example --dry-run
```

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

**Low coverage** — read `phase_seconds` in `menutree_rows.json`. Runs end on
the time budget, so the largest phase is the biggest coverage lever.
