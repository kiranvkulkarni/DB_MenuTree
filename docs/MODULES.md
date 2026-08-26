# Code Map

Where each thing lives and what it is responsible for. Pair with
[ARCHITECTURE.md](ARCHITECTURE.md), which explains *why*, and
[STATE_OF_PLAY.md](STATE_OF_PLAY.md), which says what currently works.

There are **four** back-ends in the tree, built in this order. Only the last
two matter for the deliverable; A and B are kept because their downstream
(coverage, path emission, the graph model) is still shared.

| | Back-end | Question it answers | Status |
|---|---|---|---|
| A | DroidBot | what screens exist? | superseded |
| B | replay explorer | what screens exist, deterministically? | superseded |
| C | element-tree walker | what *elements* exist, by depth? | works, not reproducible |
| D | **verifier** | does the build match the authored sheet? | **the gate** |

C and D share every layer except traversal. That is the whole design: the
expensive, hard-won parts — element enumeration, the action guard, screen
identity, the workbook writer — are traversal-agnostic.

## Data flow

```
   +--------- back-ends A/B: graph discovery (superseded) ------------+
   device -> droidbot -> utg.js -> utg_parser.py ---+
   device -> replay_explorer.py -> menutree.json ---+-> MenuTree -> path_emitter
                                                    |            -> coverage.py
   +------------------------------------------------+

   +--------- back-end C: element discovery --------------------------+
   device -> element_tree.py -> menutree_rows.json --+
                                                     |
   +--------- back-end D: verification (the gate) ----+---------------+
   MenuTree.xlsx -> spec_reader.py -> SpecRow[] -> verifier.py -> RowResult[]
                                                     |
                                                     v
                             menutree_workbook.py -> output/<run>/*.xlsx
                             tree_uvta.py         -> output/<run>/*.uvta
```

Both C and D write into a **per-run folder**, `output/<package>_<stamp>/` or
`output/verify_<package>_<stamp>/`, stamped to the millisecond. Nothing is
ever written over a previous run.

## Core model

| File | Responsibility |
|---|---|
| `src/parser/menu_tree.py` | `MenuTree`, `MenuState`, `Transition`, `Selector`. BFS from root, reachability, dead ends, ambiguity classification. **Back-end agnostic.** |
| `src/parser/selectors.py` | `SelectorResolver` — view to selector, in the required priority **text, description, resource id, xpath**, including Compose descendant-label resolution. |

## Device layer — shared by C and D

| File | Responsibility |
|---|---|
| `src/crawler/hierarchy.py` | uiautomator XML to normalised views. **`state_key`** (the state abstraction), `foreground_package` (which app is in front, read free from the dump), `EMPTY_STATE`, `looks_like_dialog`, `center_of`. |
| `src/crawler/elements.py` | `enumerate_elements` — every element, not just clickable ones. `Element.annotated()` renders the sheet's `[Title]` / `(On/Off)` notation. `screen_similarity` for screen identity. |
| `src/crawler/device_driver.py` | `DeviceDriver` protocol; `U2Driver` (uiautomator2) and `AdbDriver` (no-agent fallback). **Owns device lifecycle**: `prepare_device` / `release_device` / `launch_clean`. |
| `src/crawler/action_guard.py` | Refuses to click destructive, outbound, account and commerce controls. Presets plus `--guard-extra`. Guarded rows are reported, never pressed. |
| `src/run_lock.py` | **One run per device.** A live lock refuses a second run and names the PID; a stale one is taken over. |

## Back-end C — element-tree walker (discovery)

| File | Responsibility |
|---|---|
| `src/crawler/element_tree.py` | Worklist traversal over `(screen, element)`. Records `pending / done / unreachable / blocked / recorded`, giving a coverage denominator. Relaunch-and-replay navigation, keypad/MMI protection, incident capture. |
| `tools/build_menutree.py` | CLI. Per-run folder, guard flags, `--clear-between-paths`, `--no-reset`, `--skip-walk`. |
| `tools/compare_runs.py` | Two-run comparison: metrics side by side with better/worse markers, plus where the clock went. Warns when a run is still live, because a mid-run checkpoint reads as a finished result. |

## Back-end D — verifier (the gate)

| File | Responsibility |
|---|---|
| `src/verify/spec_reader.py` | Reads the hand-authored workbook back into a tree. Depth is **positional** (the `N Depth` columns); a row's parent is the nearest preceding shallower row. Strips `[Title]`/`(On/Off)` annotations; carries `[bracketed]` rows as **context**, not as clicks. |
| `src/verify/matching.py` | **The depth columns are not selectors** — they are a tester's English. Scores a spec label against each element's label and resource id, tolerating typos, dropped nouns (`Flash icon`), elisions and paraphrase, while still rejecting `Photo` vs `Video`. Emits `alias_review.json`; `--aliases` feeds confirmed mappings back. |
| `src/verify/verifier.py` | Walks the spec top to bottom, exploiting shared path prefixes. Position tracked **by index, not by label** — the sheet repeats `ON`, `OK`, `Back key icon` at many depths. Emits `Pass / Fail / NA / NT`. |
| `src/verify/matching.py` | **The depth columns are not selectors** -- they are a tester's English. Scores a label against each element's text and resource id, tolerating typos, dropped nouns, plurals, quantity notation and elision, while still rejecting `Photo`/`Video`. Emits `alias_review.json`. |
| `tools/drift_report.py` | Groups failures by branch and classifies each renamed / restructured / absent. Reads a partial checkpoint, so a long run can be triaged while running. |
| `tools/build_spec_xlsx.py` | Rebuilds the workbook from rows transcribed out of photographs (`tools/menutree_ocr/`), preserving the original row numbers. |
| `tools/verify_menutree.py` | CLI. `--dry-run` parses the spec with no device attached. Writes a **copy** of the workbook; the original is never touched. Exit code 2 on failures. |

## Shared downstream

| File | Responsibility |
|---|---|
| `src/generator/menutree_workbook.py` | The deliverable: single sheet, depth columns, `Needs Manual Test`, UVTA in the last column, result dropdowns. |
| `src/generator/menutree_sheet.py` | The depth-column layout itself, shared by the workbook writer and the verifier's write-back. |
| `src/generator/tree_uvta.py` | Element-tree rows to UVTA test cases (back-ends C/D). |
| `src/generator/path_emitter.py` | Graph paths to `TestCase`s (back-ends A/B). Deterministic ordering. |
| `src/generator/uvta_syntax.py` | **Every piece of UVTA surface syntax.** Change output here and nowhere else. Verified against the cheat sheet; unconfirmed forms marked `UNVERIFIED`. |
| `src/generator/uvta_writer.py` | Writes and structurally validates the suite. Refuses to write an empty suite. |
| `src/analysis/coverage.py` | Coverage report, baseline diffing, gate evaluation. |
| `src/llm/inference.py` | Optional. **Naming and grouping only** — never in the structural path. |
| `src/logging_setup.py` | Timestamped stream plus per-run file logging. |

## Tests

All run without a device, in seconds. Run them before any device work.

| File | Covers |
|---|---|
| `tests/test_hierarchy.py` | XML parsing, state-key stability, Compose selectors, selector priority, bounds |
| `tests/test_run_lock.py` | Mutual exclusion per device, stale-lock takeover, `--force-lock`, corrupt lock files |
| `tests/test_matching.py` | Sheet-wording drift that must match, different controls that must not, resource-id matching, alias round trip |
| `tests/test_navigation.py` | The rise-then-descend plan, including the sibling case and the depth invariant |
| `tests/make_fixture.py` | Writes a synthetic `droidbot_out/` in DroidBot's exact format |
| `tests/make_spec_fixture.py` | Writes `tests/spec_fixture.xlsx`, shaped like the real workbook — bracketed context rows, annotations, depth to 7 |

```bash
python tests/test_hierarchy.py
python tests/test_run_lock.py
python tests/make_spec_fixture.py
python tools/verify_menutree.py --spec tests/spec_fixture.xlsx --package com.example --dry-run
```

The last command is the fastest way to exercise the spec reader end to end:
it needs no device and prints the reconstructed tree.

## Device lifecycle — read before changing it

A run **must** hand the device back, however it ends.

`prepare_device()` wakes the screen, dismisses the keyguard and sets
`svc power stayon true`. Without it, a screen that times out mid-run destroys
the walk silently: the OEM camera swaps its whole UI for a "Tap to show
preview" placeholder, so the walk keeps running and finds nothing.

`release_device(package)` is the exact inverse, and does three things: drops
`stayon`, force-stops the app under test, and presses Home.

All three matter. Dropping `stayon` alone leaves the app open, mid-menu, on a
lit screen — and an OEM camera left open keeps auto-focusing, running scene
detection and animating its viewfinder. From across the desk that is
indistinguishable from the tool still clicking, which is exactly what it was
mistaken for.

Two rules follow, and both are load-bearing:

- `release_device` runs from a **`finally`**, never only on the success path.
  A run killed by a timeout or Ctrl-C used to leave `stayon true` set
  permanently, so the device stayed lit indefinitely afterwards.
- `_release()` is **idempotent**. It is called from both the walker and the
  CLI's `finally`, and must not fail on the second call.

## Throughput is coverage

Runs end on the clock with most of the worklist untouched, so a cheaper
action is directly more coverage. Every run writes **`phase_seconds`** into
`menutree_rows.json`: seconds, calls, mean ms and percent of run, per phase.

**Read it before optimising anything.** Three of the four things that looked
expensive were not — a hierarchy dump costs 0.11s, while the innocuous-looking
`current_package()` cost 0.41s and 23% of a run. ARCHITECTURE §12.55 has the
measurements and what came of them.

Two traps: a **mid-run checkpoint is not a result** (check
`output/.run-lock-<serial>` — if it exists the run is still going), and a
crashed walk is now printed loudly and exits 1 rather than looking like a
successful empty run.

Before and after any change that touches traversal, run:

```bash
python tools/compare_runs.py --package <pkg>     # the two most recent runs
```

Three navigation changes on this project were shipped without that
comparison and all three made coverage worse. It costs one command.

## Common tasks

| Task | Go to |
|---|---|
| Change UVTA output syntax | `generator/uvta_syntax.py` |
| Change the workbook layout | `generator/menutree_sheet.py` |
| Change what counts as "the same screen" | `hierarchy.py :: state_key`, `elements.py :: screen_similarity` |
| Make a run faster (which raises coverage) | read `phase_seconds` in the stats first, then `element_tree.py :: _await_stable` |
| Change how a view becomes a selector | `parser/selectors.py` |
| Change what is refused as unsafe | `crawler/action_guard.py` |
| Change how the authored sheet is read | `verify/spec_reader.py` |
| Change how a spec row is judged | `verify/verifier.py :: _verify_row` |
| Change how sheet wording maps to screen text | `verify/matching.py` — and prefer recording an alias over loosening a threshold |
| Add a device back-end | implement `DeviceDriver` in `device_driver.py`, **including `release_device`** |
| Add an action type (scroll, text input) | `elements.py` (enumerate) plus `element_tree.py` (`_perform`) plus `uvta_syntax.py` (render) |

## Config

`config.yaml.example` is the annotated reference. Local copies
(`config.yaml`, `config.*.yaml`) are gitignored.

The settings most worth understanding before a run:

- `return_similarity` (default **0.55**) — how alike two screens must be to
  count as the same one. Not a guess: of 40 recorded identification failures
  at 0.75, 32 scored 0.6 to 0.7 and were demonstrably the same screen.
- `clear_between_paths` — `pm clear` between navigation paths. Slower, but it
  removes cross-path state bleed.
- `reset_before_start` — start from a fresh copy of the app. On by default.
- `guard_presets` — which classes of action are refused. Read the safety
  section of ARCHITECTURE.md before widening these on a personal device.
