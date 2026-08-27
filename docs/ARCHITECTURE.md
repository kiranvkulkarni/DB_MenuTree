# MenuTree AutoQA — Architecture and Design Notes

This document explains **why** the system is shaped the way it is. The code
comments explain what each piece does; this explains the reasoning, the
alternatives that were rejected, and the traps that are easy to fall back
into. Read this before making changes.

**[METHOD.md](METHOD.md) is the companion**: how the system was arrived at,
the mistakes that shaped it, and what to be careful of when extending it.
This document is the *what and why*; that one is the *how, and what it cost*.

---

## 1. The problem

Produce, for an Android app, an exhaustive and **reproducible** enumeration of
every screen and every interactive option, expressed as replayable test cases,
with a coverage number trustworthy enough to gate a product release.

Two properties matter more than raw discovery count:

1. **Reproducibility.** The gate compares each run against a baseline. If the
   crawler is nondeterministic, a coverage drop is ambiguous — app regression,
   or did the crawler just wander somewhere else this time? An ambiguous gate
   is an ignored gate.
2. **Verifiability.** A test case that has never been executed is a
   hypothesis, not a test.

Everything below follows from those two.

---

## 2. The central decision: nothing infers the structure

The original pipeline flattened the crawler's output into a linear click
stream and asked an LLM to infer which clicks belonged to which UI branch.

**This cannot work, in principle.** A flat stream has no parent pointers.
Given `click "Flash"` → `click "Auto"` → `click "Resolution"`, nothing in the
stream says whether Resolution was reached from inside the Flash submenu or
after backing out. The model is being asked to recover information that was
discarded.

Worse, the information was never missing. The traversal knows which screen it
was on when it pressed a control; discarding that and asking a model to
reconstruct it is throwing away the answer and then paying to guess it.

So the architecture is:

```
crawler ──► state graph ──► BFS paths ──► test cases     (deterministic)
                                │
                                └──► coverage ──► gate
                    LLM: test-case *naming* only, failure-tolerant
```

The same crawl data always produces a **byte-identical** `.uvta` file. This is
verified: two runs produced the same SHA-256. The suite header deliberately
contains no run id, timestamp, or absolute path, so CI can diff it.

> **If you change one thing, do not put a model back in the structural path.**
> There is no LLM in this codebase and no LLM SDK installed. The structure
> comes from the traversal, which knows it; a model asked to infer it from a
> flat stream is being asked to recover what was already discarded.

---

## 3. One traversal, two questions

DroidBot and the replay explorer were the first two attempts and are gone
from the tree. What replaced them is a single element-tree walk, used two
ways:

| | question | entry point |
|---|---|---|
| Discovery | what is in this build? | `tools/build_menutree.py` |
| **Verification** | does this build match the sheet? | `tools/verify_menutree.py` |

They share every layer except how they choose what to visit: element
enumeration, selector resolution, the action guard, screen identity, device
lifecycle and the workbook writer are common. §12.5 describes the walker,
§13 the verifier.

**Why the first two were dropped.** DroidBot's graph had to be reconstructed
from `utg.js` plus a directory of event files, and its exploration was
timing-dependent, so the same build did not give the same graph. The replay
explorer fixed determinism by replaying each path from a clean launch, but
still answered the wrong question: it produced a *screen graph*, and the
MenuTree deliverable is an *element tree by depth*. Both were also a
dependency and a patch step that the current tool does not need — see §13 for
what verification made possible that neither could.

Also rejected, and worth not revisiting:

- **AutoDroid** is a DroidBot fork whose installed entrypoint removed
  `-policy` and hardcodes an LLM task-directed policy. It pursues one
  natural-language goal and stops when it believes it is done — the opposite
  of exhaustive coverage.
- **Fastbot / Kea2** are stability fuzzers. Excellent at shaking out crashes,
  but stochastic, which is directly at odds with baseline diffing.
- **Google App Crawler** is discontinued; **Firebase Robo** is cloud-only and
  needs an uploaded APK, so it cannot reach a preinstalled OEM camera. See
  [TOOL_EVALUATION.md](TOOL_EVALUATION.md).

---

## 6. State abstraction — the knob that decides everything

`src/crawler/hierarchy.py :: state_key`

This decides what counts as "the same screen".

```
too coarse → distinct screens merge, coverage silently under-reports
too fine   → volatile content explodes the graph, runs stop agreeing
```

Three modes; **`affordance` is the default**:

| mode | includes | use |
|---|---|---|
| `content` | all text | most precise; explodes on live data |
| `affordance` | structure + text of interactive elements | default |
| `structure` | structure only | coarsest; merges distinct screens |

**Why `affordance`.** The target app shows a live gold rate and "Updated just
now". Under `content`, every dump mints a new state and no two runs agree.
Under `affordance`, display text is excluded but the text of anything
clickable/checkable (or within 3 levels below one) is kept, because that is
what distinguishes "purity dropdown showing 22K" from "…showing 18K".

Verified in `tests/test_hierarchy.py`:

```
volatile display text does NOT change the key   (rate 15589 → 15612: same key)
affordance text DOES change the key             (22K → 18K: different key)
```

**Editable fields are excluded.** An `EditText`'s text is user data, not a
label. Including it minted a new state for every value: clicking fields holding
`10.0`, `2.0`, and a live rate produced 8 spurious states in one run. The field
contributes `<editable>` instead of its contents.

**`EMPTY_STATE`.** If every view is filtered out (launcher frame, system
dialog, pre-render), the hash of zero parts is `sha256("")` — *identical for
every such screen*, silently merging them into one bogus state. Observed as
root state `e3b0c44298fc…`. Now returns an explicit sentinel that can never
become a state.

### 6.1 Never compare state counts across abstractions

Two crawlers, or two settings of `state_key_mode`, will disagree wildly on
"how many screens" — not because one explored better, but because they are
counting different things. A text-sensitive key inflates: every price change
or clock tick is a new state.

If you need to compare, re-hash the *same saved hierarchies* with both keys.
Comparing the headline numbers from two different runs is meaningless, and
was the source of one confidently wrong conclusion here.

## 7. Compose and the selector problem

`src/parser/selectors.py` — shared by both back-ends so they cannot drift.

Jetpack Compose emits a bare clickable `android.view.View` with no text,
content-description, or resource-id. The label sits on a **child** node:

```
View  clickable=true   text=None  desc=None     ← the thing you tap
 └─ View  clickable=false  desc="Refresh GOLD rate"   ← the label
```

Measured on a real screen: **only 3 of 16 clickable views carried any
identifier of their own.** Without subtree resolution every Compose control
collapses to `className "View"`.

`SelectorResolver` walks the subtree breadth-first (default depth 4). One
crawl resolved 34 selectors this way; another, 1818.

**Ambiguous selectors are reported, not emitted.** A `className` selector
cannot uniquely identify anything on a screen with dozens of bare `View`
nodes. Such edges are counted as `ambiguous_transitions` — a coverage gap —
rather than turned into tests that would click an arbitrary match.

---

## 8. Timing: quiescence, not sleeps

Compose lays out asynchronously. Capturing as soon as the app is merely
on-screen catches a partial tree: **57 views against the 80 the settled screen
has**, producing a different state key on every launch. Every frontier item was
then discarded as drift — the explorer found 1 state and took 0 actions.

The fix is not a longer sleep. `_await_stable()` polls until **two consecutive
dumps agree on the key**, and is applied after every action, not just at
launch, because transitions animate too.

> A fixed sleep is a guess that is simultaneously too slow and occasionally too
> short. Wait for the observable to stop changing.

---

## 9. Coverage and the gate

`src/analysis/coverage.py`. Exit codes: `0` pass, `1` pipeline error, `2` gate
failed.

**Two different numbers, deliberately:**

- **Crawl breadth** — activities the crawler physically reached.
- **Testable coverage** — activities reachable from launch by UI steps alone.
  **This is what the gate uses.** An activity the crawler stumbled into but
  that no emitted test can reach is not covered; counting it would overstate
  the gate, which is the wrong direction to be wrong in.

The gap is reported as *discovered but not testable* and usually means a screen
reachable only via deep link, restart, or an unreproducible state.

**Activity names are normalised.** Android reports `.MainActivity`; the
manifest declares `com.example.app.MainActivity`. Unnormalised, the set
difference never matches and the app's own main screen is reported as
unreached — observed. Normalised in `MenuState.__post_init__`.

### 9.1 Activity coverage is a poor metric for modern apps

The target app declares 3 activities, one of which is real; the other two are
`androidx.activity.ComponentActivity` (a base class) and
`PreviewActivity` (Compose tooling). Neither is launchable. So the report reads
**33% activity coverage while covering 100% of real screens.**

**For Compose / Navigation-Component apps, gate on `states_discovered` and
`actionable_transitions` regression, not activity percentage.**

---

## 10. Open problems

Honest list, roughly by importance.

0. **Apps with variable launch state defeat replay exploration.** The Samsung
   camera restores its last-used mode, so four launches produced four
   different root states (127, 136, 145, 145 views). Replays then land
   somewhere other than where the path was recorded and are discarded:
   **89% drift, 2 states, 4 tests from a 900s budget** — against 22 states
   and 77 tests for the Phone app in the same budget.

   `clear_between_paths` is the fix, but on the camera `pm clear` is itself
   imperfect (first-run tips then vary: 2 distinct states across 3 clears)
   and it destroys the user's camera preferences, so it is unusable on a
   personal device.

   Compounding it, the camera needs ~30s to settle per launch against the
   dialer's ~2s, so each frontier item costs 40-60s. Replay exploration
   trades speed for determinism and **the exchange rate is set by app
   startup cost**; for a camera it is a poor trade.

   Options, in order of preference: run on a test device with clearing on and
   a 30-60 minute budget; or a hybrid that backtracks within a screen and
   only replays when a branch genuinely needs restoring; or simply keep
   a backtracking crawler for this app, since it pays no restart cost.

1. **State-abstraction tuning is unfinished.** The editable-field fix has not
   been measured on a completed run — the emulator died mid-run. Until that
   number exists, treat explorer state counts as provisional.
2. **Self-loop edges.** 66 of 155 edges were actions that changed nothing.
   Legitimate edges, weak test cases, and they inflate the test count. Decide
   whether they count as coverage; probably flag separately.
3. **UVTA grammar is partly inferred.** Only `launch`, `click`, and
   `verify … exists timeout` are confirmed. Everything else in
   `src/generator/uvta_syntax.py` is marked `UNVERIFIED`. **Correct that one
   file before trusting a gate result** — no other module hardcodes UVTA text.
4. **Semantic input.** Fields are enumerated but never typed into, so whole
   subtrees behind a validated form are invisible. If a model is ever worth
   introducing, it is here and nowhere else — and only as a fallback, after a
   screen has been revisited N times with nothing new found. Never in the
   structural path.
5. **Screen identity.** The real limiter; see §12.56. Upstream of navigation,
   coverage and reproducibility alike.

---

## 11. Safety: crawling can perform real actions

An exhaustive crawler presses every button it finds. On the Phone app it
**dialled numbers** — 55 of 68 discovered states were `InCallActivity`. On an
emulator the modem is simulated and this is harmless. **On a physical device
it would place real calls.**

Before crawling on real hardware, consider what the app can do irreversibly:
place calls or send messages, spend money, send email, delete user data,
change system settings, or post to a network service. Mitigations:

- Prefer an emulator for apps with outbound side effects.
- Use a device with no SIM, in aeroplane mode, or on a test account.
- Bound the crawl with `max_depth` and keep out-of-app detection strict.
- Review the discovered activity list after a first short run before
  committing to a long one.

This is not hypothetical — it happened during development, on the first
system app tried.

---

## 12. Environment traps

Real time was lost to these.

- **Emulator clock skew wedges the guest.** After a host sleep, the guest clock
  jumped backwards ~80 minutes; logcat timestamps ran backwards and a GC pause
  read `18446744030s` (a uint64 underflow). `system_server` died. `adb reboot`
  does not fix it — the clock does not resync.
- **A force-killed emulator leaves stale locks.** Delete
  `hardware-qemu.ini.lock` and `multiinstance.lock` in the AVD directory or the
  next launch segfaults.
- **A uiautomator2 NPE on `registerUiTestAutomationService` usually means
  `system_server` is dead**, not that your Android version is unsupported.
  uiautomator2 works fine on Android 17 / SDK 37.
- **Use software rendering for emulator gate runs.** This emulator hung three
  times under hardware GPU and has been stable since
  `-gpu swiftshader_indirect`. If you gate on emulators rather than physical
  devices, this is the difference between a usable and an unusable pipeline.
- **This emulator crashed three times under crawl load.** Prefer physical hardware
  for gate runs. The explorer now checkpoints every 5 states so a crash costs
  minutes, not the whole run.

---

## 12.5 The element-tree walker (current MenuTree path)

`src/crawler/element_tree.py`, driven by `tools/build_menutree.py`.

The deliverable is a tree of **elements by depth**, not a graph of screens.
A state graph collapses a filter list into one node; the sheet wants one row
per filter. See `elements.py` for enumeration and the `[Title]` / `(On/Off)`
annotations.

### Navigation: tap, don't only go BACK

BACK is the wrong verb for tab-based UIs. Leaving the dialer's Keypad tab
lands on the call log; BACK again exits to the launcher. Tabs are re-entered
by **tapping them**. Recovery order is therefore:

1. BACK once, verify by element overlap.
2. Still in-app but wrong screen → **re-click** the element we descended
   through. Must be tried here, before pressing BACK again: repeated BACKs
   walk further away, and an earlier attempt placed this check after the
   loop, where the launcher was already in front and it never fired.
3. Only then relaunch and replay (~30s).

Measured effect of adding step 2: BACK ok/failed 8/8 → 24/2, relaunches
8 → 2, descents 2 → 8, rows 69 → 107.

### Read `lost_returns`, not `back_failed`

`back_failed` counts "BACK didn't work, a fallback ran" — a **cost** metric.
`lost_returns` counts "the walk lost its place" — the **health** metric, and
it has been 0 throughout. Four debugging rounds were spent treating the cost
metric as a failure metric. Depth was limited by relaunch time, never by
correctness.

### Safety findings from real runs

- **The walker executed USSD/MMI codes.** It pressed dialpad keys until
  Android auto-ran a code, leaving the app in `com.android.phone`. Keypad
  keys (`^[0-9*#+]{1,3}$`) are now recorded as rows but never pressed. Some
  vendor codes are destructive — `*2767*3855#` is a factory reset on Samsung.
  The danger is not in any single label but in the **accumulated sequence**,
  which a label-pattern guard cannot see.
- **The guard was bypassed by identifier labels.** Underscores are word
  characters, so `call` never matched `end_call_fab_test_tag`. Labels
  are now split on separators and camelCase before matching.
- **ANR and crash dialogs** are detected and recorded as incidents. An ANR
  found during a crawl is a defect the run surfaced, not noise to swallow.
- **The keyboard was being enumerated** — one tap into a search field added a
  row per key. The active IME is resolved from the device and excluded.

### Known limits

- Best depth reached is 8, against an expected 18. (Early runs reached only
  4; the wake, clear-propagation and identity-threshold fixes below moved it.)
- **Discovery is not reproducible.** Identical code, device and app produced
  465 / 291 / 90 rows. This is the reason the verifier exists — see §13.
- Data-heavy screens contribute records as rows (call log entries). Fixed
  option lists — filters, resolutions — come out correctly, which is what the
  camera sheet needs, but user data and fixed options are not distinguished.
- Specification knowledge is unreachable by crawling: `on 3rd entry of
  camera`, `[When location Permission is OFF in DUT]`, `[Rear Camera]`.
  A crawler can draft structure and types; those annotations cannot be
  observed on a screen.

### Screen identity: 0.55, and why it is not a guess

Two screens count as the same when their element sets overlap by
`return_similarity`. The threshold began at 0.75, which was too strict: of 40
recorded identification failures, **32 scored 0.6–0.7 and were demonstrably
the same screen** — the near-misses are logged in `identify_misses` precisely
so this could be measured rather than argued about. Lowering it to 0.55 took
one Realme run from 306 to 400 rows, identification misses from 40 to 4, and
relaunches from 57 to 22.

Comparison is stem-normalised, because OEM labels encode their own state:
`filteroff` becomes `filteron` when you press it, so a screen scored 0.25
against itself. `_label_stem` strips the trailing state suffix before
comparing.

---

## 12.55 Throughput *is* coverage

The most important fact about coverage, and the least obvious:

> **The good runs do not stop because the walker gets lost. They stop
> because the clock runs out.**

Measured over eight Realme runs, every run that reached meaningful depth
ended with `elements_pending > 0` — the time budget expired with most of the
worklist never attempted. The best run had **154 of 224 actionable elements
never tried**. Its 25% coverage was `56 done / 224 actionable`; the other 69%
was not a failure to navigate, it was a failure to get there in time.

So making an action cheaper *is* raising coverage, and the phase timer
(`phase_seconds` in the stats) exists to say which action to make cheaper.
Do not optimise without reading it — three of the four things that looked
obviously expensive were not.

### What the timer actually said

First measurement, 128s run:

| phase | seconds | % of run |
|---|---|---|
| `await_stable` | 97.6 | **76%** |
| ├ `current_package` | 29.7 | 23% |
| ├ `dump` | 29.0 | 23% |
| └ `sleep(0.4)` | 38.9 | 30% |
| `tap` | 4.3 | 3% |

The hypothesis going in was that `dump_hierarchy` dominated, because it
parses a whole screen. It does not: a dump costs **0.11s**. Parsing and
state-keying are free (1ms and 0ms). Three real costs turned up instead.

**1. A blind sleep after every tap.** `tap()` did `click()` then
`time.sleep(settle)` — 1.00s of a 1.27s tap. Every one of those taps is
immediately followed by `_await_stable()`, which polls the screen properly.
The sleep was pure redundancy, and it is the exact anti-pattern §8 warns
about, sitting inside the driver. `tap(settle=False)` now skips it; the
default stays `True` for callers with no quiescence loop.

**2. `current_package()` costs 0.41s — four times a whole dump.** And the
dump already carries a `package` on every node, because uiautomator dumps the
focused window. `foreground_package(views)` reads it for free: the package
owning the most views, excluding OS chrome (the status and navigation bars
are on every screen and would otherwise win on a sparse one).

Measured against the authoritative call on 24 settled screens: **24 agreed,
0 disagreed**. On *unsettled* screens it lags a transition by a frame — an
early test caught a dump still showing the camera while the Gallery came up —
so it is only resolved after quiescence, and a *foreign* answer is still
confirmed with the real call before anything acts on it. Walking a foreign
app as if it were ours has cost real debugging here (§12.5), so that one
answer is worth 390ms.

**3. The quiescence gap was flat.** A settle needs 2.28 dumps on average,
barely above the minimum of two — so nearly every screen is already still on
the second look, and a fixed 0.4s gap just pays 0.4s to confirm it. The gap
now starts at 0.12s and doubles, capped at 0.8s. Fast screens settle in a
third of the time; a screen that is genuinely still animating gets *more*
patience than before by its fourth poll, which is the right trade.

### Result, same 120s budget, same device and app

| | before | after |
|---|---|---|
| clicks | 22 | **36** (+64%) |
| rows | 180 | **216** (+20%) |
| `await_stable` mean | 1284ms | **642ms** (−50%) |
| `current_package` | 29.7s (23%) | 1.3s (1%) |

The next-largest phase is now `relaunch_clear` at 24.5% — 3.07s per relaunch,
10 of them. Note that `launch_clean(clear=True)` and `clear=False` measured
3.10s and 3.13s, so **the cost is the launch, not the `pm clear`**. Reducing
it means relaunching less often, which is a navigation problem, not a timing
one.

### Two traps when measuring this

**A mid-run checkpoint is not a result.** The walker checkpoints
`menutree_rows.json` as it goes, so reading it while a run is live gives a
plausible, wrong, smaller answer. This has already caused one false "fix
works" on this project. Check `output/.run-lock-<serial>` before believing a
number — if the lock is there, the run is still going.

**A crash used to look like a successful empty run.** A `NameError` in the
walker was caught, logged, and reported as "1 row, 0 clicks, exit 0" — which
reads as "this app has no UI" rather than "this tool is broken". Partial
walks are still kept, but the failure is now printed loudly and exits 1.

---

## 12.56 Navigation, and the limiter underneath it

Both paths are known, so getting from one screen to another is always the
same shape: **rise to the deepest screen the two paths share, then descend.**
`navigation_plan()` is that rule, extracted as pure arithmetic so
`tests/test_navigation.py` can cover it without a device.

One rule replaces the two special cases that preceded it, and adds the one
that was missing:

| here | target | plan | |
|---|---|---|---|
| `A>B>C` | `A>B>C>D` | `(0, [D])` | descend |
| `A>B>C` | `A>B` | `(1, [])` | rise |
| `A>B>C` | `A>B>D` | `(1, [D])` | **sibling** |
| `A>B>C` | `X>Y` | `(3, [X,Y])` | rise to root, descend |

The sibling case is the one that matters. In a depth-first walk the usual
next move is to a sibling -- finish `Flash > On`, now do `Flash > Off` -- and
it matched neither old branch, so every one fell through to a relaunch and
full replay. The baseline run recorded `nav_forward 3, nav_back 3` across
**176 clicks**: six shortcuts in the entire run, with 69 relaunches costing
34.7% of the clock.

**Why rising is safe.** `rises` never exceeds our own depth (asserted in the
tests), so BACK only ever retraces a descent we made, and the deepest it can
land is the root screen -- it is the *next* press that would leave the app,
and that one is never issued. Two further guards: `_click_label` fails if the
label is not on the screen in front of us, so a BACK landing somewhere
unexpected can never cause a blind click; and arrival is verified against the
target's elements, falling through to replay if it did not land.

An early version refused any plan that rose all the way to the root, fearing
unrelated paths. That rejected the cheapest and commonest move of all -- a
top-level sibling, Photo to Video to Portrait -- and sent it to a relaunch.

### Rejected: re-planning mid-climb

Worth recording because it looks obviously right and is not.

One BACK is genuinely not one level: an overlay like `SubSet` collapses
several at once, so a planned two-press climb can overshoot. The natural fix
is to rise one press at a time, identify where you landed, and re-plan.

Measured, it was worse. The trace showed it re-planning from bad data -- one
BACK from `filteroff > SubSet` reported as landing on `Front Camera`, a
different branch entirely -- and `identify_misses` hit its cap of 40 in a
single run, against 8 without it. **Re-planning from a misidentified screen
picks a confidently wrong route.**

### The real limiter: screen identity

`back_ok 5` against `back_failed 64`. Shortcuts land where expected about
**10%** of the time, and that is not a fault of the climbing strategy.

The trace says why: the `SubSet` screen appears beneath many different
parents carrying the same 26 elements. Element-set similarity -- the whole
basis of `screen_similarity` and `return_similarity` -- cannot distinguish
those instances, so "where am I?" returns a plausible wrong answer.

Until identity separates them, no path-based navigation can be reliable, and
roughly a quarter to a third of every run will keep going to relaunches.
That is the next thing to fix, and it is upstream of navigation, coverage,
and reproducibility alike. Note it likely needs something *other* than the
element set -- the activity name, the scroll position, or the path taken to
arrive -- because the element sets are genuinely identical.

---

## 12.57 Selectors, and what the emitted suite must say

### Which handle a control offers is not our choice

Whoever built the app decided whether a control exposes visible text, a
content-description, a resource id, or nothing at all. So a selector is
*read* per element from the XML dump, in the confirmed order:

```
1. text        2. description        3. resource id        4. xpath
```

xpath is structural and always available, which is why it is the final
fallback rather than one of the priority keys.
`src/parser/selectors.py` is the only place this order is decided, and the
resolved selector travels `Element` -> `TreeNode` -> `menutree_rows.json` ->
the emitter. Path steps carry their own, captured as the walk descends: a
child screen's path selectors are its parent's plus the element actually
pressed to reach it.

**Why it must travel rather than be re-derived.** A row's label is whichever
of text or content-desc was non-empty. An emitter that turns a label back
into `text "<label>"` produces, for every icon, a selector that cannot match
anything — `verify text "Back key icon" exists` against a control whose only
handle is `desc "Navigate up"`. That was about 30% of a 2000-case suite, and
nothing in the row count, depth or coverage showed it.

### A verify must prove the click before it

The emitter used to click a step and then assert that same step still
existed:

```
click text "Quick settings"
verify text "Quick settings" exists      <-- proves nothing
```

A menu item that opens a submenu is usually replaced by it, so this passed
vacuously when the item happened to stay on screen and failed spuriously when
it did not. Neither outcome establishes the one thing the step exists for:
that the click worked.

Assertions are chained instead — click A, verify B; click B, verify C — so
each one proves the preceding click landed by asserting what that click was
supposed to reveal:

```
launch "com.sec.android.app.camera"
click desc "Quick controls"
verify text "Filters" exists
click text "Filters"
verify desc "Take picture" exists
```

A case that passes has demonstrably walked its whole path.
`tests/test_uvta.py` asserts both properties, including that no verify checks
the element just clicked.

---

## 12.58 Discovery scrolls, or it inventories only the visible part

The walker enumerated one hierarchy dump per screen, so a list taller than
the display was recorded only as far as it happened to be visible — the
Samsung camera's settings screen carries 45 labels and shows about twelve.

Two halves, and doing only one is worse than neither:

- `_enumerate_scrolled` collects elements at each scroll position,
  deduplicated, then **rewinds to the top** so the caller sees the screen it
  expects.
- `_find_element_scrolled` scrolls to reach a control when the walker returns
  to press it. Without this, rows below the fold are recorded and then
  reported *unreachable* — losing exactly the rows scrolling was added to
  find, while making the coverage figure look worse.

The swipe geometry lives in `hierarchy.py` (`scrollable_container`,
`swipe_span`) and is shared with the verifier. That sharing is deliberate: a
fixed-span swipe produced a negative coordinate on a short container and
killed a two-hour run, and one copy of that fix is enough to maintain.

---

## 12.6 Device lifecycle and concurrency

Two rules that are easy to get wrong and expensive to debug, because both
failure modes look like something else entirely.

### A run must hand the device back, however it ends

`prepare_device()` wakes the screen, dismisses the keyguard, and sets
`svc power stayon true`. This is not a convenience. A screen that times out
mid-run destroys the walk *silently*: the OEM camera replaces its entire UI
with a "Tap to show preview" placeholder under `.setting.ScreenOffActivity`,
leaving one element to explore. The walk keeps running and finds nothing —
and because the outcome then depends on *when* the screen happened to time
out, this is a strong contributor to the 90-vs-465 variance.

`release_device(package)` is the exact inverse and does three things:

1. drops `stayon`
2. force-stops the app under test
3. presses Home

All three matter. Dropping `stayon` alone leaves the app open, mid-menu, on a
lit screen — and an OEM camera left open keeps auto-focusing, running scene
detection and animating its viewfinder. Observed from across a desk that is
indistinguishable from the tool still clicking, and it was reported as
exactly that.

**`release_device` must run from a `finally`.** It used to run only on the
success path, so a run killed by a timeout or Ctrl-C left `stayon true` set
permanently and the handset lit indefinitely. `_release()` is idempotent
because both the walker and the CLI call it.

### One run per device

`src/run_lock.py` holds a lock file per device serial in the output root. Two
runs driving one handset interleave their taps, and the damage is invisible
in the logs — each reports a plausible walk while the other navigates
underneath it. It also explains a device that appears to act after a run
"finished": an earlier run with a two-hour budget is still going, and the
finished one is not the one you are watching.

A live lock refuses the new run and prints the PID to kill. A stale one — the
owning process is gone — is taken over silently. `_alive()` is deliberately
conservative: a PID it cannot classify counts as alive, because wrongly
declaring a lock stale is worse than a spurious refusal. `--force-lock`
overrides.

---

## 13. Verification: walking the sheet instead of discovering it

Discovery must solve preconditions, data-vs-menu discrimination, depth-18
traversal *and* reproducibility. Verification needs none of them.

The 1,896-row sheet is hand-authored and repeatedly verified — it is a
specification, not a discovery output. So the job was never "discover the
tree"; it is "walk the known tree and assert each row is present".

| problem | under discovery | under verification |
|---|---|---|
| reproducibility | unsolved blocker | gone — same rows every run |
| preconditions | impossible to infer | gone — the sheet states them |
| coverage denominator | grows as you explore | fixed at 1,896 |
| navigation failure | silent lost coverage | a Fail on a named row |

### Reading the sheet back into a tree

Two properties of the layout make this reliable, and both are load-bearing:

- **Depth is positional.** A row's depth is which `N Depth` column holds its
  label, so structure is explicit rather than inferred from indentation.
- **Rows are in tree order.** The sheet is depth-first, so a row's parent is
  the nearest preceding row one level shallower. No ids needed.

The header is located by looking for the depth columns rather than a fixed
row index, so the summary block at the top of the real workbook — and any
drift in its height — does not break the parse.

Three cases cost real debugging:

**Depth 1 is the application, not a control.** Left in the path, every route
began with a "Camera" tap that matches nothing on screen, and *every row*
failed with `path step not found`. It is satisfied by the app being up.

**`[Bracketed]` rows are context, not clicks.** `[When location Permission is
OFF in Dut]` states a precondition. It is carried as context for the rows it
qualifies and reported `NA` with the precondition quoted — a human runs it.
Crucially, such a marker scopes to its **siblings** as well as its
descendants: it sits at the *same* depth as the Cancel / Turn on rows it
governs, so discarding same-depth context detaches the precondition from the
rows that need it.

**Annotations are not on-screen text.** `[Title]`, `(On/Off)`, `(Radio button
On/Off)` are notes to the reader. They are stripped before the label is used
as a selector, and kept for the report.

### Track position by index, not by label

The verifier exploits the sheet's depth-first order: consecutive rows usually
share a path prefix, so the common case is stepping forward one level rather
than relaunching. That requires knowing where you currently are.

Position is tracked as `path[:step + 1]` — an index — **never** by searching
the path for the label just clicked. The sheet repeats `ON`, `OK` and `Back
key icon` at many points and depths, so `path.index(label)` resolves to the
first occurrence, which is usually the wrong depth, and corrupts the
shared-prefix calculation for every subsequent row.

### The depth columns are not selectors

The single most important thing to understand about the workbook.

A depth cell is **how a manual test engineer described the control, in their
own English, while looking at the phone.** It is not the on-screen text, and
frequently nothing like it:

| the sheet says | the screen says | |
|---|---|---|
| `Flash icon` | `Flash` | describes rather than names |
| `Back key icon` | `Navigate up` | no shared words at all |
| `Priorize quality` | `Prioritize quality` | typo |
| `JEPG format` | `JPEG format` | typo |
| `High efficiency pitures` | `High efficiency pictures` | typo |
| `Scene Optimiser` | `Scene optimizer` | spelling variant |
| `Location tags ...recorded.` | the full sentence | author elided it |
| `while using this app` | `While using the app` | paraphrase |

Exact matching -- or exact-then-substring, which is what this started with --
reports **Fail on controls that are present and working**. For a release gate
that is the worst error available: it manufactures defects, and a gate that
cries wolf gets switched off.

`src/verify/matching.py` scores a spec label against every element on screen,
on both the visible label and the resource id (developer English is often
closer to tester English than the UI text is, and it is the only handle on an
icon with no text at all). Every match carries a **score and a reason**.

The two errors are not symmetric, and the thresholds reflect that:

- **≥ 0.80 confident** — treated as found.
- **0.60–0.80** — treated as found, but the workbook comment is prefixed
  `REVIEW WORDING:` and names what it actually matched.
- **< 0.60** — not found.

Tolerance must not become blindness. `Photo`/`Video` scores 0.10,
`12MP`/`50MP` 0.25, `ON`/`OFF` 0.20 — all correctly rejected. The tightest
real pair is `Front Camera`/`Rear Camera` at **0.56** against a 0.60
threshold: correctly rejected, but the margin is thin, and pairs like that
are the best argument for recording an alias rather than trusting the score.

### The durable fix is an alias file, not a better heuristic

No heuristic should stay permanently responsible for deciding that
"Priorize quality" meant "Prioritize quality". A person should decide once,
and every run after that should be exact and auditable.

So every inexact match is written to `alias_review.json` in the run folder.
A reviewer sets `confirmed: false` on anything wrong, and the file is passed
back with `--aliases` on the next run, where a confirmed alias beats any
guess outright. Matches below `CONFIDENT` are written with
`confirmed: false` already set, so a weak guess is never silently promoted
into a permanent mapping — someone has to say yes.

This is the mechanism by which the tool converges: the first run is fuzzy and
noisy, and each review makes the next one sharper and quieter.

### A precondition is not a reason to skip a row

The first version returned `NA` for any row carrying a `[bracketed]`
context. A dry run against a real workbook killed that immediately:

> **3 context rows put a precondition on 963 of 1052 verifiable rows.**

A marker like `[Rear Camera]` sits high in the sheet and legitimately
qualifies everything beneath it, so 91% of rows inherited one. The verifier
would have checked 89 rows and skipped the rest, which is not a gate.

Most such preconditions are ambient state the app already satisfies on
launch. So a row with context is now attempted like any other, and context is
used only to interpret a **miss**:

| | outcome |
|---|---|
| context, element found | **Pass** |
| context, element missing | **NA**, naming the precondition |
| no context, element missing | **Fail** |

The reasoning is that we cannot distinguish "the build lost this control"
from "the precondition did not hold", so calling it a Fail would be a lie —
but refusing to look at all wastes the row entirely.

This is worth remembering as a general shape: the fixture was built to
imitate the real workbook and had 3 context rows out of 74, where the real
one had 3 out of 1055. **The ratio, not the feature, was what broke it** —
and only real data showed that.

### The original workbook is never modified

Results are written to a copy, `<name>_verified.xlsx`, in the per-run folder.
Exit code 2 on any failure.

---

## 14. Extending it

**Adding a crawler back-end.** Produce a `MenuTree` (or write
`menutree.json`, format `menutree/1`, and use `MenuTreeLoader`). Everything
downstream is back-end agnostic. The only coupling is the event-type
vocabulary in `menu_tree.py` and `path_emitter.py` — a small enum mapping.

**Adding a device back-end.** Implement the `DeviceDriver` protocol in
`device_driver.py`. Exploration targets the protocol only. `AdbDriver` exists
as a no-agent fallback for Android versions the uiautomator2 agent lags.
Implement `release_device` properly — see §12.6; a driver that does not hand
the device back leaves the handset lit with the app running.

**Changing UVTA output.** Edit `src/generator/uvta_syntax.py`, nothing else.

**Changing state abstraction.** Edit `state_key` in `hierarchy.py`, then
re-run the §6.1 re-hash to see the effect on a known corpus *before* spending
15 minutes on a device run.

**Testing without a device.** All of these run in seconds — use them first.

- `tests/test_hierarchy.py` — parsing, state keys, Compose selector resolution.
- `tests/test_run_lock.py` — per-device mutual exclusion and stale-lock
  takeover. Note the comment in it: the first version of that test planted a
  PID that had already exited, so the lock correctly treated it as stale and
  the test passed for the wrong reason. Liveness tests need a live process.
- `tests/make_spec_fixture.py` — writes `tests/spec_fixture.xlsx`, shaped like
  the real deliverable, so the spec reader can be exercised without it. Then
  `python tools/verify_menutree.py --spec tests/spec_fixture.xlsx --package
  com.example --dry-run`.
