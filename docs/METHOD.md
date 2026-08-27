# How this was built, and how to take it forward

`ARCHITECTURE.md` explains *what* the system does and why it is shaped that
way. This explains *how it was arrived at* — the method, the mistakes, and
what to be careful of when extending it.

It is written for the next person, who will not have watched any of it
happen. Nearly every rule here exists because something went wrong first, and
the evidence is kept with the rule so you can judge whether it still applies.

---

## 1. The method: instrument, measure, fix, verify

One pattern held for the whole project, without exception:

> **Every fix found by reading a trace worked the first time.
> Every fix reasoned out from a plausible story made things worse.**

Three navigation "improvements" were shipped on reasoning and all three were
reverted — deterministic ordering (465 rows → 38), tracking the current
screen (→ 15), a cheap re-click before relaunch (465 → 84). Each was
convincing on paper. Each was shipped *before* running the comparison that
would have caught it.

So the loop is:

```
    instrument  ->  measure  ->  change ONE thing  ->  measure again
                                        |
                                   worse? revert, and read WHY it got worse
```

**Instrumenting is cheaper than being right.** The phase timer took twenty
minutes to write and immediately refuted the hypothesis it was built to
confirm: a hierarchy dump costs 0.11s, while `current_package()` — which
looked trivial — cost 0.41s and 23% of a run. Without it the obvious
optimisation would have been the wrong one.

**Change one thing.** When two changes go in together and the result is
worse, you have learned nothing except that one of them is bad.

**A worse result is data, not failure.** See §3.5.

---

## 2. Two questions, two tools

| | question | tool |
|---|---|---|
| **Discovery** | what is in this build? | `tools/build_menutree.py` |
| **Verification** | does this build match the sheet? | `tools/verify_menutree.py` |

Discovery came first and is the larger body of code. It is **not**
reproducible — identical code, device and app produced 465, 291 and 90 rows —
and that is disqualifying for a gate: a build that "loses" 150 rows might be
a regression or might be Tuesday.

The reframe that fixed it: **the sheet is a specification, not a discovery
output.** It is hand-authored and repeatedly verified. So the job was never
"discover the tree"; it is "walk the known tree and assert each row is
present". That makes the row set identical every run, fixes the denominator
before the run starts, and turns a navigation failure into a named Fail
rather than silently missing coverage.

Discovery keeps one narrow job: finding rows the build has that the sheet
does not. That is a report, not a gate.

---

## 3. The learnings

### 3.1 A false green is the only unacceptable result

The first full Modes run printed:

```
PASS RATE : 100.0%      (18 Pass, 102 NA, 0 Fail)
```

The gate had judged 18 of 120 rows and called the build perfect.

The cause was a fix of mine that looked more careful than what it replaced.
Context rows (`[Rear Camera]`) were NA-ing rows before they were attempted;
I changed it so a preconditioned row *is* attempted but a **miss** still
became NA, reasoning that a missing control under an unmet precondition is
not evidence about the build. Sound in isolation — but `[Rear Camera]` sits
high in the sheet, **989 of 1078 rows inherit it**, and every genuine failure
became NA.

Two things now guard against it:

- **A precondition does not excuse a miss.** Only preconditions the tool
  genuinely cannot establish qualify, and that list has exactly one entry
  (`permission`), justified by measurement — see §5.1.
- **The summary reports how many rows were actually judged**, and says
  loudly when that is under 60% that the percentage is not a gate result.

**Why this is the worst failure mode:** a gate that cries wolf gets switched
off. A gate that says "all clear" while looking at nothing gets *trusted*.

**How nearly it slipped past:** 18 Pass / 102 NA / 0 Fail looks *better* than
the Settings sheet's 49 / 28 / 11, and "100%" reads like success. Nothing in
the number hinted at the problem. It was caught only because I had said in
advance that I wanted to check whether those NAs were genuine.

### 3.2 A false Pass is worse than a false Fail

They are not symmetric, and the matcher's thresholds encode that:

- A **false Fail** manufactures a defect. Someone spends an hour disproving
  it. Annoying, visible, self-correcting.
- A **false Pass** reports a control as working that nobody looked at. It is
  invisible, and it is exactly what the gate exists to prevent.

Two real ones, both caught by reading the `detail` text of rows that had
*succeeded* — which is not where you normally look:

| spec row | matched | why |
|---|---|---|
| `On` | `Exposure monitor` | "on" sits inside m-**on**-itor |
| `Pro [Tittle]` | `T` | a lone letter |

Containment now requires a contiguous run of **whole words**. And two
single-quantity labels with different values (`1x` vs `15x`) score 0.05
outright rather than being judged on spelling, because SequenceMatcher rated
them 0.80 and that landed above the match threshold.

### 3.3 Silence is the dangerous failure

A garbled result gets noticed. A *missing* one does not — it just shrinks the
denominator, and the pass rate looks fine.

- **OCR dropped whole columns.** Tesseract returned nothing at all for the
  `2 Depth` and `6 Depth` columns of a page that plainly showed
  `Quick settings`, `Default`, `Roboto`, `Noto Serif`. That is why the sheet
  was transcribed by reading rather than by OCR.
- **`read_sheet` kept only the first cell in a row.** 26 rows of the Modes
  sheet carry a parent and its child on one line (`8K` + `30`,
  `Center` + `Adjust bar`). All 26 second cells vanished without a word.
- **A header collision dropped a column.** Writing the reconstruction with
  the header on row 5 let the first data row overwrite the `1 Depth` header:
  87 rows instead of 88, no error. The row-count check caught it.
- **A crash reported as success.** A `NameError` inside the walk was caught,
  logged, and summarised as "1 row, 0 clicks, exit 0" — which reads as "this
  app has no UI" rather than "this tool is broken".

**The countermeasure is always the same: count something and compare it to
something you know independently.** Settings' 88 transcribed rows against the
sheet's own summary of 88. Rows read against rows skipped. Judged rows
against rows checked.

### 3.4 Measure before optimising

Coverage sat near 25% and the assumption was that navigation was losing
screens. The stats said otherwise: **every run that reached real depth ended
with the clock expired and most of the worklist never attempted** — one had
154 of 224 actionable elements never tried. Its 25% was `56 done / 224`.
The missing 69% was not bad navigation, it was not getting there in time.

So throughput *is* coverage, and the phase timer says which action to make
cheaper. It contradicted every guess:

| suspected | actual |
|---|---|
| hierarchy dump is expensive | 0.11s — not the problem |
| `current_package()` is trivial | 0.41s, **23% of the run** |
| `tap()` is a tap | 1.00s of its 1.27s was a blind sleep |
| quiescence needs a 0.4s gap | 2.28 dumps settle the average screen |

Result at the same budget: 22 → 36 clicks, `await_stable` 1284ms → 642ms.

### 3.5 A regression can be the finding

Invalidating the tracked position after a failed navigation was **correct**.
It also dropped passes 11 → 3 and drove relaunches 1 → 76.

That regression is what exposed the real bug. `launch_clean` only
force-stopped a *foreign* app, so when the camera itself sat three screens
deep in its own settings, `am start` merely resumed the task and returned to
the screen it was asked to leave. **All 76 "relaunches" were no-ops.** The
first fix was right; it just made an existing bug visible by exercising it 76
times instead of once.

**Do not revert a correct change because the number went down.** Read why.

### 3.6 The spec is a human document

The depth columns are **not selectors**. They are how a manual test engineer
described a control, in their own English, while looking at the phone:

| the sheet says | the screen says |
|---|---|
| `Flash icon` | `Flash` |
| `Back key icon` | `Navigate up` |
| `Priorize quality` | `Prioritize quality` |
| `Quick settings` | `Quick controls` |
| `0.6x` | `.6` |
| `2sec` | `2S` |

Exact matching reports Fail on controls that are present and working. So
matching is deliberately tolerant, every match carries a **score and a
reason**, and weak matches are flagged `REVIEW WORDING` in the workbook
rather than accepted silently.

The sheet also has *conventions*. `Each mode depth as below` tells a human
that the modes listed after it are reached through the menu the marker hangs
under. Read literally it cost **281 failures across seven modes that scored
zero passes** — 35% of every failure in the sheet — for controls one tap
further in. The convention is reasonable; the tool has to understand it.

**The durable answer is not a cleverer heuristic.** No scoring function
should stay permanently responsible for deciding that `Priorize quality`
meant `Prioritize quality`. Every inexact match is written to
`alias_review.json`; a person confirms it once and from then on the mapping
is exact and auditable.

### 3.7 Check the artefact where it is used, not where it is made

Two faults sat in the emitted UVTA suite for the whole project and were
invisible to every measurement taken:

- **Every control was addressed as `text`.** A row's label is whichever of
  `text` or `content-desc` was non-empty, so each icon got
  `verify text "Back key icon" exists` -- a selector that cannot match
  anything at runtime. About 30% of the suite.
- **Every `verify` asserted the element that had just been clicked.** A menu
  item that opens a submenu is usually replaced by it, so the assertion
  passed vacuously when the item happened to remain and failed spuriously
  when it did not. It never established what it existed to establish: that
  the click worked.

Neither showed up in row counts, depth, coverage, or any test. The workbook
was right. The suite was the right *size*. Every number said the deliverable
was fine, because **every number measured the tree, and the bug was in the
translation from tree to test.**

Both were found by a person reading the output and asking what a line would
actually do on a device. That question is not answerable from the metrics,
and no amount of instrumenting the walk would have surfaced it.

> Measure the thing you produce, at the point someone consumes it. A
> deliverable that is the right shape and the right size can still be
> uniformly wrong.

Concretely, for this repo: `tests/test_uvta.py` now asserts that no `verify`
checks the element just clicked, and that an icon is addressed by its
description. If the emitter changes, run a case from the suite mentally
against a screen before trusting the row count.

---

---

## 4. Guardrails, and what each one is scar tissue from

| guardrail | what happened |
|---|---|
| `RunLock`, one run per device | two runs interleaving taps corrupt both invisibly; also explains a device that "keeps acting" after a run |
| `release_device()` in a `finally` | an interrupted run left `stayon true` set, so the handset stayed lit indefinitely |
| release also force-stops and goes Home | an OEM camera left open keeps auto-focusing — indistinguishable from the tool still clicking |
| checkpoint every 25 rows | a two-hour run wrote results only at the end, and the interrupt handler read an attribute nothing ever set |
| loud failure + exit 1 on a partial walk | a crash was being reported as a successful empty run |
| judged-rows line + false-green warning | see §3.1 |
| action guard, keypad/MMI protection | an exhaustive crawler presses every button; on the Phone app it dialled numbers, and `*2767*3855#` is a factory reset |

---

## 5. Extending it: what to be careful of

### 5.1 Anything that converts a Fail into an NA

This is the highest-risk category of change in the codebase, because it makes
the number go up while making the gate weaker.

`UNESTABLISHABLE` currently holds one term, `permission`, and it earns its
place by measurement: `pm clear` does not revoke a preinstalled camera's
permissions (they are `GRANTED_BY_DEFAULT`), and `pm revoke` and
`pm reset-permissions` both fail with `SecurityException` for shell. Once
Android records the decision, `Only this time` and `Don't Allow` are never
offered again.

**Anything added here needs that standard of evidence** — a measurement
showing the state cannot be established, not a hunch that it might not have
been. Widening this list is precisely how the 100%-over-15% result happened.

### 5.2 Anything that loosens the matcher

Every loosening must be checked in **both** directions. The habit that
catches errors is to list what must now match *and* what must still not:

```
must match      0.6x/.6   1x/1   Timer 2sec/BACK_TIMER_2S   Flash icon/Flash
must not match  1x/15x    12M/50M   ON/OFF   Photo/Video   On/Exposure monitor
```

`tests/test_matching.py` holds both lists. Add to both when you touch
scoring. Three separate loosenings (stemming, quantity folding, containment)
each introduced a false positive that only the negative cases caught.

### 5.3 Thresholds are measured, not chosen

- `return_similarity = 0.55` — of 40 recorded identification failures at
  0.75, **32 scored 0.6–0.7 and were demonstrably the same screen**.
- `CONFIDENT = 0.80`, `REVIEW = 0.60` — the band between them is deliberate:
  matched, but flagged for a human.

If you change one, re-run the two-run comparison (`tools/compare_runs.py`)
rather than reasoning about it.

### 5.4 App-agnosticism is a property to keep testing

The logic contains no app knowledge — every constant list is a generic
Android concept, and "camera"/"Samsung" appear only in comments. Discovery
runs against any package; it was demonstrated unmodified on the Clock app
(293 rows, depth 9).

But **tuning leaks in unnoticed**. The scroll code used a fixed 900px swipe
centred on the container — an assumption from one app's tall settings list.
On a container near the top of the screen `centre_y - 450` went negative,
uiautomator2 asserts `y >= 0`, and a two-hour run died outright. It now
derives the swipe from the container's own box.

Expect one or two more of these on a structurally unusual app. They are
shallow fixes, not architectural ones — but they are invisible until a
different layout hits them.

### 5.5 Two traps that will waste your afternoon

- **Stale `__pycache__` silently runs old code.** It happened twice, and both
  times the conclusion drawn was wrong. Runs now use `python -B`.
- **A mid-run checkpoint is not a result.** The walker and verifier both
  write partial results as they go. Reading one while a run is live gives a
  plausible, smaller, wrong answer — this caused one false "the fix works".
  Check `output/.run-lock-<serial>` first; if it exists, the run is still
  going.

### 5.6 Selectors: one place decides, and it is not the emitter

`src/parser/selectors.py` owns the order -- **text, description, resource id,
xpath** -- and is the only place it is decided. Which handle a control offers
is entirely up to whoever built it, so it has to be read off the XML dump per
element; xpath is structural and always available, which is why it is the
final fallback rather than one of the priority keys.

The resolved selector is carried from enumeration through `Element` ->
`TreeNode` -> `menutree_rows.json` -> the emitter. If you add a step to that
chain, carry it: the emitter re-deciding the selector from a label is exactly
the bug described in §3.7.

Path steps carry their own selectors too, captured as the walk descends -- a
child screen's path selectors are its parent's plus the element actually
pressed to reach it.

### 5.7 Where the spec's conventions live

`MODE_SHORTHAND` in `spec_reader.py` encodes *this team's* sheet convention,
not a universal one. On another sheet it simply will not fire. A different
shorthand needs its own rule, and that rule belongs next to this one with the
same comment style: what it means, and what it cost to get wrong.

---

## 6. Still unsolved

**Screen identity is the real limiter.** `back_ok 5` against
`back_failed 64` — navigation shortcuts land where expected about 10% of the
time. The reason is visible in the trace: the `SubSet` screen appears beneath
many different parents carrying the same 26 elements, and element-set
similarity cannot tell those instances apart, so "where am I?" returns a
plausible wrong answer.

A refinement that re-planned the route after each BACK was **rejected on
evidence** for exactly this reason: one BACK from `filteroff > SubSet` was
reported as landing on `Front Camera`, and `identify_misses` hit its cap of
40 in a single run.

Until identity separates those screens, no path-based navigation can be
reliable and a quarter to a third of every run keeps going to relaunches. It
likely needs something *other* than the element set — the activity name, the
scroll position, or the path taken to arrive — because the element sets are
genuinely identical.

That is the change most likely to move navigation, coverage and
reproducibility at once, and it is where I would start.
