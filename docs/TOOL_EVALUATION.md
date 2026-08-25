# Evaluating Google App Crawler and Firebase Robo for MenuTree

Both were evaluated against one question: **can either produce, or verify, a
1,896-row element tree for a preinstalled OEM camera app on our own test
handset?**

Short answer: **neither can.** One is gone; the other cannot reach the app.

---

## 1. Google App Crawler — discontinued

The tool that looked most promising. It ran locally, and its termination
condition ("no more unique actions to perform") is exactly the
worklist-exhaustion semantics built by hand in this repo.

**It no longer exists.**

| check | result |
|---|---|
| `developer.android.com/studio/test/other-testing-tools/app-crawler` | **404** |
| `developer.android.com/studio/test/app-crawler` | **404** |
| `developer.android.google.cn/.../app-crawler` | **404** |
| `androidx/test/tools/crawler` in Google Maven | **404** |
| `androidx/test/crawler` in Google Maven | **404** |
| `crawl*` anywhere in Google's master Maven index | **no match** |

Only third-party blog posts remain, all describing a `crawl_launcher.jar`
downloaded from the now-dead docs page. Google's Maven `androidx.test` group
index lists `orchestrator` and the other test artifacts but no crawler.

**Verdict: not viable.** Nothing to download and nothing maintained. Even if
a copy of the jar surfaced, betting a release gate on an unmaintained,
undocumented binary would be worse than what we have.

---

## 2. Firebase Test Lab Robo — alive, but cannot reach our app

Robo is well maintained and genuinely good at what it does. It produces
exactly the artefact shape we want: a **crawl graph** with screens as nodes
and actions as edges, plus annotated screenshots, video and logs.

Three blockers, any one of which is fatal here:

**It is cloud-only.** Robo runs on Google's managed device farm. There is no
local runner. Our target is a specific OEM camera on a specific handset in
our lab.

**It requires an uploaded APK.** There is no mode that targets an app already
installed on a device. The Realme/Samsung camera is a preinstalled *system*
app — signed with the platform key, granted permissions by default, and
dependent on vendor services. Uploading its APK and installing it as an
ordinary app on a Test Lab device would not reproduce the app under test,
assuming it installed at all.

**Timeouts are short.** 300s default (console/Studio), 900s via gcloud. Our
runs need 25 minutes and still exhaust their budget.

**Verdict: not viable for the OEM camera.** Worth keeping in mind for any
*normal* app that ships as an installable APK — for that case it is likely
better than anything we would build.

### One part worth stealing

**Robo scripts** are a JSON format describing a sequence of UI actions, used
to drive the crawler to a specific place before it explores. That is the same
shape as the precondition problem (`on 3rd entry of camera`), and the format
is documented. Even without the runner, it is a reasonable model for encoding
"get into this state first".

- [Robo test (Android)](https://firebase.google.com/docs/test-lab/android/robo-ux-test)
- [Robo scripts reference](https://firebase.google.com/docs/test-lab/android/robo-scripts-reference)

---

## 3. What this evaluation actually settles

I recommended looking at these because I suspected building a crawler from
scratch was the wrong path. The evaluation confirms the instinct and refutes
the remedy: there is no off-the-shelf crawler that fits a preinstalled OEM
app on our own hardware.

But it also removes the reason to keep chasing crawler reproducibility —
because of what we now know about the deliverable.

**The 1,896-row sheet is hand-authored and repeatedly verified.** It is a
specification, not a discovery output. Which means the job was never
"discover the tree". It is:

> walk the known tree, assert each row is present, record Pass / Fail.

That reframing dissolves the three problems that have consumed this work:

| problem | under discovery | under verification |
|---|---|---|
| Reproducibility (90–465 rows/run) | unsolved blocker | **gone** — the same rows are walked every run |
| Preconditions (`on 3rd entry`) | impossible to infer | **gone** — the sheet states them |
| Coverage denominator | grows as you explore | **fixed** — 1,896 rows, known up front |
| Drift / navigation failure | loses coverage silently | becomes a **Fail on a named row** |

Under verification, a navigation failure stops being noise and becomes the
signal: if the expected row cannot be reached, that is a defect worth
reporting, which is exactly what a gate is for. And the pass rate is
meaningful on day one — 1,892 / 1,896 means something; "32.8% of what I
happened to find" does not.

---

## 4. Recommendation

Build the verifier, reusing what already works here:

- element enumeration, selector resolution (text → desc → id → xpath)
- the action guard, keypad/MMI protection, IME exclusion
- ANR/crash detection as incidents
- the workbook writer, including `Needs Manual Test`
- UVTA emission per row
- per-run output folders

Replace only the traversal: instead of discovering what exists, read the
expected tree from the workbook and verify it.

Discovery still has one narrow, valuable job — **finding rows the build has
that the sheet does not**, so the specification can be updated when the app
gains UI. That is a report, not a gate.

The parts of this repo worth keeping are the safety layer, the output
formats, and the hard-won platform knowledge in `ARCHITECTURE.md`. The
traversal engine is the part to replace.
