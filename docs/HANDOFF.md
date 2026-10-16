# Handoff — state on 2026-10-16

Written for whoever continues this work, a person or Claude. Read
[../CLAUDE.md](../CLAUDE.md) for the rules and commands, then this file.
[HANDLING.md](HANDLING.md) is the agreed policy. [STATE_OF_PLAY.md](STATE_OF_PLAY.md)
describes the tool in August 2026 and is history now.

## 1. Where it stands

The walker (`tools/build_menutree.py`, `RecursiveWalker`) produces a full
MenuTree of the Samsung camera on the S25 Ultra in about 2.5 hours. Every
part was built screen by screen with the user and checked on the phone:

| Step | Area | State |
|---|---|---|
| 1 | Settings, from Quick controls > Go to Settings | Done. 171 of 177 sheet rows found, 167 at the sheet's depth |
| 2 | Watermark: governed rows, Edit, Font, Alignment | Done |
| 3 | Quick controls: panels, captions, selection groups | Done |
| 4 | Filters: carousel walked item by item, editors listed, selection restored | Done |
| 5 | Modes: PORTRAIT, PHOTO, VIDEO, MORE, each MORE tile as a node | Done |
| 6 | Front camera as its own pass (`walk_separately`) | Done |
| 7 | First-launch pop-up chain, after clearing the app | Done |
| Review 1 | 14 comments on run 2026-10-15 05:07, applied tree-wide | Done (HANDLING.md, "First review") |
| Review 2 | Run 2026-10-16 00:33 reviewed; PRO dial panels fixed | Done (commit 7fee317) |

The latest full run is in §4. Its workbook is the one handed over for the
next review.

## 2. Decisions agreed with the user

All of these are in HANDLING.md with their reasons. The short list:

- **Switches:** governed rows go under `On`. The switch's own description
  stays beside Off / On. The phone's own wording is accepted (BACK_TIMER_2S).
- **Widgets:** a radio, switch or checkbox sits in the cell beside its item. A
  switch goes beside the last line of its row. An item's current value is
  written directly under the item.
- **A control named by its description** is written "name | state", for
  example `Resolution | BACK_CAMERA_PRO_PICTURE_SIZE_NORMAL`. Dynamic text
  stays as the phone shows it, for example `Customize text | Edit`.
- **Only what is new:** under a screen or panel opened from another, nothing
  still showing from the screen behind is repeated. That includes readings
  that changed meanwhile.
- **Filters:** select each filter and restore the selection. Editors are
  listed with their sliders unmoved. Download filters is recorded only
  (`leaves_app`).
- **Modes:** each MORE tile is a node. Another app (Gallery, Store, Bixby) is
  listed one level deep, once. Edit is list-only. Settings is walked in full
  once; later modes list it and open only new rows (`--revisits top`, a CLI
  option).
- **Front camera:** under its own node, `Switch to front camera`. A mode with
  no front version (FOOD, PANORAMA) is a noted leaf.
- **First launches:** a node before PORTRAIT. Every answer branch is pressed;
  the walking state is then set by Turn on, While using the app, No thanks.
  Suggestions ("Use Night mode") are listed, never pressed.
- **PRO dials:** controls plus the scale values seen. AF modes are pressed,
  recorded and put back. A panel's or bar's Reset is listed, never pressed.
- **One profile file per package** (`profiles/<package>.json`). An app with no
  profile is walked on structure alone.

## 3. Open decisions (the user's, not the code's)

1. **`S25U_MenuTree.xlsx` and `tests/spec_fixture.xlsx` are in git and on
   GitHub** (branch `feat/utg-derived-menutree`). The first is a
   reconstruction of the device team's MenuTree, typed from photographs, so it
   is company material. Options: stop tracking it from now on
   (`git rm --cached`), and/or remove it from history and force-push. Also
   check whether the GitHub repo is private.
2. **The work is on branch `feat/utg-derived-menutree`, not `main`.** It was
   pushed to GitHub on 2026-10-16. Whether and when to merge it into `main`
   is the user's call.
3. **Manual-mode PRO controls.** The walk no longer turns a dial, because
   that changes a setting. So the controls that exist only in manual mode are
   no longer recorded: the focus areas CENTER / MULTI / MANUAL, "Manual ISO",
   the bar's Reset chip and the manual white-balance controls. The user
   earlier agreed to "press and record" the focus areas. Seeing them needs the
   dial's Auto button pressed (it switches to manual) and pressed back. Ask
   whether to do that.
4. **Labels that come and go on their own.** "Bar" (a divider in PRO VIDEO's
   top bar) and "F 1.7" (the aperture readout, shown for a moment after a
   change) are listed as `[Title]` or "could not be located". Proposed rule:
   ignore a label that appears and disappears with nothing pressed. Not
   agreed yet.
5. **"Turn on Improve location accuracy?"** (Not now / Turn on) appeared once
   while probing, outside the first-launch chain. "Not now" was pressed by
   hand. If it appears in a walk, its answer should be agreed and added to
   `launch_answers`.

## 4. Latest full run

`output/com.sec.android.app.camera_20261016_081232_667/` (output/ is not in
git; the user has the workbook):
- 3,655 rows, 3,409 UVTA cases, 2 h 7 min
- no warnings, no failed returns; all 104 switches restored; 27 relaunches
- the four PRO passes (PRO and PRO VIDEO, rear and front) each list their
  five dials the same way, and no setting was changed

Its `REVIEW_GUIDE.md` lists the known gaps the user was given.

The previous run, `..._20261016_003319_417`, was reviewed by the user
("worked on and is how I expected").

## 5. Known gaps and next steps

In order:

1. Review the latest workbook with the user (sheet `MenuTree_To_Be`,
   column R), apply the comments tree-wide, and re-walk the branches they touch.
2. Settle the open decisions in §3. Items 3 and 4 are code once agreed;
   item 1 is git housekeeping.
3. **Wrong "blocked" comments.** `Reset camera settings? [Title]` carries
   "blocked … route not walked", but only the Reset button was refused; a
   title row should never carry `blocked`. Separately, the title and
   description of PORTRAIT > … > Photo format > Pro mode picture format say
   "blocked (format)" because the walk briefly took that page for a pop-up and
   checked every label strictly.
4. **NIGHT, intermittent:** in the 08:12 full run and in one branch walk, the
   shutter-time chip ("4s") and "Back" were not listed. Four later walks of
   NIGHT listed both. The cause is not found. The walk logs nothing for them,
   and they are on the MORE screen in neither case. Catch it with
   `MENUTREE_SAVE_DUMPS=<folder>`, which saves the dump each screen was listed
   from, then compare a failing dump with a good one.
5. **A control whose text is a live value** (NIGHT's "1s"/"4s", a dial's
   level) changes between being listed and being pressed, giving "could not
   be located". Idea: find it again by its resource id and place when the
   label has moved.
6. **VIDEO > Quick controls > Auto framing** switches Auto framing on, and the
   walk took the result for a screen; getting back needed relaunches. Proposed
   (not agreed): treat it as a state toggle with a caption, like Night shot.
   Since the stricter return check (commit 7fee317), a panel whose controls
   stay after the toggle reads as "not back yet"; watch the relaunch count.
7. Compare against the device team's real MenuTree at the company with
   `tools/compare_tobe.py` (it reads `profiles/<package>.json` aliases).
   Here only a reconstruction was available.
8. Other apps: the walker is app-agnostic, so any package can be walked; add
   a `profiles/<package>.json` only for app-specific decisions.

## 6. Moving this to another machine

- **Code:**
  `git clone -b feat/utg-derived-menutree https://github.com/kiranvkulkarni/DB_MenuTree.git`.
  If GitHub is not reachable, `git bundle create DB_MenuTree.bundle --all` on
  the old machine makes a single file you can clone from in the same way.
- **Setup:** Python 3.11, `pip install -r requirements.txt` (versions pinned
  as tested), adb on PATH, USB debugging on, and the phone unlocked with
  "Stay awake" helpful. The first uiautomator2 call installs its agent on the
  phone.
- **Not in git, and not needed:** `output/` (runs), `.venv/`, logs. The
  company's spec folder (`S25_Ultra_MenuTree/`) stays on company machines.
- **The device's serial** differs per phone; pass yours as `--serial`.
- **Phone quirks seen here:**
  - The phone dozes during long runs (the walker holds it awake while it runs).
  - A Location Accuracy prompt appears after "While using the app" unless
    Google Location Accuracy is off.
  - The uiautomator2 agent sometimes returns empty dumps after video
    recording; the driver restarts it.
  - The walker holds portrait orientation while it runs.

## 7. History

`git log --oneline` on `feat/utg-derived-menutree` tells the story; each
commit message says what was observed on the phone and what changed. From
step 1 to now:

```
7fee317 PRO dials: never swipe or tap a slider; readings are not new rows
cb16f4a A switch named like its item goes beside that item
2e24554 First review: widgets beside items, values under items, only what is new
2c77c84 A press that brings controls to walk opens a screen even when little moved
4edb3d2 Rows say 'blocked' only when the walk refused them; relabels only on a screen that stayed
7dcefc9 Priming answers late pop-ups and is verified before the main walk starts
2a7d882 Full run fixes: mode strip is chrome, tabs entered reliably, orientation-proof scrolling
759c45f First launches: wait for each answer to appear before pressing it
a8589e6 First launches: record every pop-up answer branch, then walk in the agreed state
0f2a1ce Back controls by whole word (Backdrop is not one); restore a carousel by any item seen
e07aa8e Options and carousel items are never taken for a relabelled control
91c744e Front camera: walk_separately controls open a separate pass; steadier state handling
3a8c0ce Modes, step 5 finished: panels by what they bring, reliable returns from modes
4af118d Modes: MORE tiles as nodes, --revisits, other apps one level, fewer relaunches
0de004d Face in PHOTO: state toggles, panels told by what they keep, values kept
3829b4b Filters: walk a selecting carousel item by item and put the selection back
ab6d068 Policy: Quick controls and Settings are dynamic per mode, walked under every mode
30a7c5c Quick controls: panels, captions, selection groups, and a reliable way home
6bfe61d A switch's description stays beside Off/On; taps land on what is seen
852ba19 Keep only the front app's elements under a prompt; fix a sweep crash on repeated labels
20bfefa Settings: switches govern below the fold, replays pass through them, restores are confirmed
```
