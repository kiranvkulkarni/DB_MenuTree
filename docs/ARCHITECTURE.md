# MenuTree AutoQA — Architecture and Design Notes

This document explains **why** the system is shaped the way it is. The code
comments explain what each piece does; this explains the reasoning, the
alternatives that were rejected, and the traps that are easy to fall back
into. Read this before making changes.

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

## 2. The central decision: the LLM is not in the structural path

The original pipeline flattened the crawler's output into a linear click
stream and asked an LLM to infer which clicks belonged to which UI branch.

**This cannot work, in principle.** A flat stream has no parent pointers.
Given `click "Flash"` → `click "Auto"` → `click "Resolution"`, nothing in the
stream says whether Resolution was reached from inside the Flash submenu or
after backing out. The model is being asked to recover information that was
discarded.

Worse, the information was never missing: DroidBot already computes the graph
and writes it to `utg.js`.

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

> **If you change one thing, do not put the LLM back in the structural path.**
> Use it for names, grouping, and prose. Never for steps.

---

## 3. Two crawler back-ends

Both produce a `MenuTree`; everything downstream is shared.

| | DroidBot (`utg_parser`) | Replay explorer (`replay_explorer`) |
|---|---|---|
| Status | **working, current gate** | prototype, better architecture |
| Exploration | greedy DFS + BACK to backtrack | replay a path from a clean launch |
| Determinism | no — BACK is timing-dependent | yes, by construction |
| Paths verified | no | yes — each was just executed |
| State abstraction | fixed, text-sensitive | ours, configurable |
| Speed | ~1 action/sec, no restarts | slower per action, restarts |

### 3.1 Why not AutoDroid, Fastbot, or Kea2

- **AutoDroid** is a DroidBot fork whose installed entrypoint *removed*
  `-policy` and hardcodes an LLM task-directed policy. It pursues one
  natural-language goal and stops when it thinks it is done — the opposite of
  exhaustive coverage. Its exploration policies are otherwise unchanged from
  DroidBot.
- **Fastbot / Kea2** are stability fuzzers. Excellent at shaking out crashes,
  but stochastic, and (as far as we know) they do not export a consumable
  transition graph. Stochastic exploration is directly at odds with baseline
  diffing.

They answer a different question and are worth running *alongside* a MenuTree
gate, not instead of one.

---

## 4. The DroidBot path: reconstruction and repair

`src/parser/utg_parser.py`.

**The join.** `utg.js` edges carry only `event_str` and `event_id`; the view
that was touched lives in `events/event_<tag>.json`. Both sides compute
`event_str` with the same function against the same state, so
`(start_state, stop_state, event_str)` is an **exact** join key. Joining gives
edges that know both structure and selector.

Then five repairs, each found by running against a real app:

1. **Root selection.** DroidBot's `<FIRST>` state is whatever was on screen
   when the crawl began — usually the launcher home screen. Its only edge into
   the app is a launch intent, which paths must never replay (every test
   already starts with `launch`). Left alone: 11 of 12 states unreachable,
   zero tests emitted. Root is now the app's entry state.
2. **Launch-transition frames.** A state can report the app's activity while
   containing *only* launcher views — captured after ActivityManager switched
   but before the app rendered. One became the graph root and prefixed every
   test with a click on a Google at-a-glance widget. Detected by view package
   and pruned.
3. **Foreign states.** Launcher and system screens pruned so they cannot
   inflate app coverage.
4. **Duplicate transitions.** DFS re-records the same control on each revisit,
   producing byte-identical tests under different names. Collapsed.
5. **Compose selectors.** See §6.

**Known unfixable-from-here:** DroidBot names event files with second
resolution (`event_%Y-%m-%d_%H%M%S.json`), so two events in the same second
overwrite each other. Observed: 1 edge lost from a 152-event crawl. The parser
warns when an edge has no matching event record. Mitigate by raising
`crawler.interval`.

---

## 5. The replay explorer: why replay instead of backtrack

`src/crawler/replay_explorer.py`.

```
frontier = [(path_to_state, unexplored_action), ...]
for each item:  launch → replay path → act → capture
```

Backtracking with BACK is stateful and timing-sensitive: it depends on BACK
landing where you assume and on no drift accumulating. Replay-from-launch
gives:

- **Reproducibility.** Fixed frontier order (FIFO) and fixed action order
  (document order) mean the same build yields the same graph.
- **Verification for free.** Every path in the graph was executed to build the
  graph. The crawl *is* the replay pass.
- **Control of capture.** State abstraction and selector resolution happen at
  capture time rather than being reconstructed and repaired afterwards.

### 5.1 Forward continuation (the performance fix)

Naively, each frontier item pays a full restart plus a replay of its entire
prefix. Measured: **5 states in 792 seconds.**

The prefix is the expensive part, so once it is paid for, keep walking forward
from wherever each action lands instead of restarting for the next one.
Ordering stays fixed, so determinism is preserved — only the restart frequency
changes. Measured after: **45 states in 901 seconds**, replays 78 → 35.

### 5.2 Replay drift — the dominant failure mode

A replay that does not land where the path was recorded is discarded, and
that branch is **never revisited**. Nothing else in the output makes this
obvious: a badly incomplete graph just looks like a small app.

Measured on the Phone app (`com.google.android.dialer`), without clearing:
**71 of 74 replays drifted.** Result: 9 states, 16 edges. With clearing:
**3 of 38**, giving 22 states and 77 edges from the same budget.

The cause is worth understanding, because the obvious guess is wrong. It was
*not* launch-state variance — measured directly, the dialer's launch state is
stable across `app_start(stop=True)`, `am start` with `CLEAR_TASK`, and
`pm clear` alike. What actually happened: the dialer shows a one-time
dismissible banner. The explorer recorded its root *with* the banner, then
dismissed it during exploration, and the app persisted "dismissed". The
recorded root became **permanently unreachable**, so everything after it
drifted.

This generalises to any one-time UI the app records as seen — onboarding, a
first-run tip, "don't show again". It poisons the root and silently collapses
the crawl.

`_warn_on_drift()` therefore logs loudly above a 30% drift rate and names the
fix. **A tool that discards 96% of its work must say so.**

### 5.2.1 One-shot dialogs are the costliest case

A modal like Samsung's "Turn on Location tags?" (Cancel / Turn on / Learn
more) is a decision point, and a release gate needs **both** branches.

The explorer already enumerates all three buttons and queues all three. The
mechanism is not the problem. The problem is that the dialog appears **once**:
answer it either way and it never returns, so replaying back to it fails and
the untaken branches are discarded as drift, permanently.

**This is not a job for an LLM.** Nothing about it is semantic — the crawler
knows exactly which buttons exist and wants to press both. It cannot get back
to the screen. The fix is state restoration (`clear_between_paths`), which
makes the dialog reappear on every replay.

Where a model *does* help, and is not yet implemented:

- Risk-classifying unfamiliar buttons. The guard is a regex list; it knows
  `Delete` and `Share` but not `Erase and continue` or vendor-specific
  phrasing. A model reading the dialog title plus button label generalises
  where patterns cannot.
- Choosing which branch to take when state cannot be restored, since only one
  is possible and they are not equally valuable.

In both the model advises; it never decides structure. See §2.

`looks_like_dialog()` detects modals structurally (dialog class hints, or a
small in-app view count with few clickables — the location-tags dialog was 35
views). Dialog states are recorded as decision points and any branch never
taken is reported as a **known** coverage gap. A silently lost branch is the
worst outcome for a gate; a named one is actionable.

### 5.3 `--clear-between-paths`

If the app persists UI state across launches (this one remembers a card's
expand/collapse), a replay from a fresh launch lands somewhere different from
where the path was recorded. The item is discarded as drift and that branch is
**lost permanently**.

With `pm clear` before each replay, drift effectively vanishes (0 on the
sample app, 3/38 on the dialer).

Slower, and it re-triggers first-run flows, but correct. **Treat this as a
correctness requirement, not a tuning option** — without it results are
silently wrong rather than merely slower.

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
| `content` | all text | closest to DroidBot; explodes on live data |
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

### 6.1 Comparing crawlers fairly

**Do not compare raw state counts between crawlers with different
abstractions.** DroidBot reported 23 states against the explorer's 5, but
DroidBot's hash is text-sensitive and inflates.

DroidBot saves full view hierarchies, so re-hash *its* states with *our*
abstraction and compare like with like:

```
scratch: rehash.py → 25 in-app DroidBot states
  content     → 24 distinct
  affordance  → 18 distinct   ← the honest target
  structure   → 16 distinct
```

That converted a vague "maybe it's just hashing" into a definite "the explorer
under-explored", which pointed straight at replay drift. **Do this whenever
comparing crawlers.**

---

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

### 9.2 Measured comparison: DroidBot vs the replay explorer

Same app (`com.google.android.dialer`), same device, same 900s budget.

| | states | edges | **usable tests** | unreachable |
|---|---|---|---|---|
| DroidBot `dfs_greedy` | 68 | 66 | **12** | 66 |
| replay explorer | 22 | 77 | **77** | 0 |

DroidBot *explored more screens* and still produced far fewer tests. Two
reasons, both instructive:

1. **Its two output artifacts disagree.** 55 of 95 UTG edge events had no
   corresponding record in `events/`, so those edges can never be given a
   selector and can never become a test. This is inherent to reconstructing a
   graph from two files written separately. The explorer captures state and
   selector together at the moment of acting, so there is nothing to
   reconcile.
2. **55 of its 68 states were `InCallActivity`** — it dialled numbers and
   explored the in-call screen (see §11 safety note).

The headline number is *usable tests*, not states discovered. A screen you
reached but cannot generate a reproducible path to is not covered.

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
   DroidBot for this app, since its backtracking pays no restart cost.

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
4. **DroidBot-path tests are still not replayed.** The explorer solves this by
   construction; the DroidBot path does not. Until then, add a replay pass.
5. **Semantic input.** DroidBot types `"HelloWorld"` into every field; the
   explorer does not type at all. Both lose whole subtrees behind validated
   forms. This is where an LLM genuinely pays off — consult it only when a
   state has been revisited N times with no new states found.
6. **Scrolling.** `interactive_views` marks scrollables but the explorer has no
   scroll action. Content below the fold is invisible to it.
7. **Split APKs.** androguard cannot read a split set as one file, so
   activities declared only in splits under-count.

---

## 11. Safety: crawling can perform real actions

An exhaustive crawler presses every button it finds. On the Phone app it
**dialled numbers** — 55 of DroidBot's 68 discovered states were
`InCallActivity`. On an emulator the modem is simulated and this is harmless.
**On a physical device it would place real calls.**

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

- **DroidBot will not install on Windows.** Defender blocks read access to
  `droidbot/resources/DroidBoxTests.apk` and the wheel build dies. Nothing
  references it — clone, delete it, install the local clone.
- **DroidBot will not start on androguard 4.x.** It imports
  `androguard.core.bytecodes.apk`, moved in 4.0. `tools/patch_droidbot.py`
  fixes this in place; **re-run it after any droidbot reinstall.**
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

- Depth reached so far is 4, against an expected 18.
- Data-heavy screens contribute records as rows (call log entries). Fixed
  option lists — filters, resolutions — come out correctly, which is what the
  camera sheet needs, but user data and fixed options are not distinguished.
- Specification knowledge is unreachable by crawling: `on 3rd entry of
  camera`, `[When location Permission is OFF in DUT]`, `[Rear Camera]`.
  A crawler can draft structure and types; those annotations cannot be
  observed on a screen.

### The alternative worth considering

Discovery must solve preconditions, data-vs-menu discrimination, and
depth-18 traversal. **Diffing against the existing sheet** needs none of
them: load the expected rows, walk the build, report what is missing, new, or
moved. That is also what the workbook actually reports — 4 fails in 1896 —
so it fits the deliverable more closely than regeneration does.

---

## 13. Extending it

**Adding a crawler back-end.** Produce a `MenuTree` (or write
`menutree.json`, format `menutree/1`, and use `MenuTreeLoader`). Everything
downstream is back-end agnostic. The only coupling is the event-type
vocabulary in `menu_tree.py` and `path_emitter.py` — a small enum mapping.

**Adding a device back-end.** Implement the `DeviceDriver` protocol in
`device_driver.py`. Exploration targets the protocol only. `AdbDriver` exists
as a no-agent fallback for Android versions the uiautomator2 agent lags.

**Changing UVTA output.** Edit `src/generator/uvta_syntax.py`, nothing else.

**Changing state abstraction.** Edit `state_key` in `hierarchy.py`, then
re-run the §6.1 re-hash to see the effect on a known corpus *before* spending
15 minutes on a device run.

**Testing without a device.** `tests/make_fixture.py` writes a synthetic
`droidbot_out/` in DroidBot's exact on-disk format.
`tests/test_hierarchy.py` covers parsing, state keys, and Compose selector
resolution offline. Both run in seconds — use them first.
