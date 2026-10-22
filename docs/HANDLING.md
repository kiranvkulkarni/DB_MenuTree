# How each kind of element is handled

The agreed policy for the MenuTree walker. Every rule here was settled by
inspecting a real screen (`tools/inspect_screen.py --probe`) and confirmed by
the sheet's author — not inferred from a failed run.

A change to the walker that alters how one of these roles is treated must
update this table first, and be re-checked with `inspect_screen.py` on a
screen that contains that role.

| role | how it is recognised in the dump | handling |
|---|---|---|
| **title** | first label inside a clickable row | press; if it opens a screen, walk it and return one level |
| **summary** | a further label inside the *same* clickable row | list at the row's depth; never press (pressing opens the row's screen and files it under the summary) |
| **switch** | `Switch` view, or a row whose title press only flips a switch | record with its state. If OFF: switch ON, walk whatever appeared, switch back OFF. Never leave a setting changed |
| **switch row** | title press changes a switch's state and nothing else appears | detected by press-and-observe; restored at once, then handled as a **switch** |
| **option** | radio choice, or an option revealed in place (Flash Off / Auto / On, 12MP / 50MP / 200MP) | list as children of the control that revealed them; never select |
| **checkbox** | `CheckBox` | list with state; do not tick |
| **slider** | `SeekBar` | list |
| **input** | `EditText` | list; do not type |
| **tab** | member of the mode strip (PORTRAIT / PHOTO / VIDEO / MORE) | a depth-2 node, entered by pressing it; never re-entered from inside another mode |
| **do-not-press** | listed in the app's profile, `profiles/<package>.json` | list; do not press. Empty for an app with no profile |
| **back** | Navigate up / Close / Back | listed on EVERY screen that has one (agreed); also the route back |
| **header** | text with no clickable row around it | list |
| **dialog** | an explicit dialog/popup class; or, failing that, a small screen whose app window does NOT fill the display (a short full-window settings page is a screen) | list title and buttons; leave by Cancel; strict action guard on its buttons |
| **overlay** | another package's window while the app's window is the larger one (judged by window area, not view count) | ignore |
| **system chrome** | the element's `package` is the platform UI (`com.android.systemui`) or the keyboard detected on the device at start-up | ignore |

## App-agnostic, by rule

The walker contains no knowledge of any particular app or vendor, and
`tests/test_recursive_walk.py` fails if a word like `camera`, `samsung` or
`shutter` appears in its code. Everything is decided from:

* **structure in the dump** -- class, `clickable`, `checkable`, the row a
  label sits in, `resource-id` groups (how the tab strip is found);
* **observation** -- press, look at what changed, restore (how a switch row
  is told from a row that opens a screen);
* **the device itself** -- which keyboard is active, which package owns each
  element.

What is genuinely specific to one app is DATA in `profiles/<package>.json`:
controls with a side effect a walk must not cause (`do_not_press`), and the
alias file mapping a sheet's wording to that app's constants. An app with no
profile is walked on structure and observation alone.

Known limit, stated rather than hidden: the words for "go back" and "decline"
(`Navigate up`, `Cancel`, `Not now` ...) are English. A device in another
language needs them in the profile.

## Sheet shape, as the To_Be sheet writes it

* **A switch that governs content** gets `Off` and `On` rows, and everything
  it governs goes under `On` — as the To_Be sheet's Watermark (J51
  Watermark, J52 Off, J53 On, K54 Info to show …).
* **`Off` / `On`** sit at the switch's own level; what the switch governs goes
  one level under `On`.
* **What a switch governs** is read from `enabled`, not from wording. A switch
  that is OFF is turned ON, and the rows that go disabled → enabled are the
  governed ones. A switch already ON (Log) is turned OFF for a moment, and the
  rows that go enabled → disabled are the governed ones. Either way the
  switch is put back as it was at once.
* **A switch's options go under `On`; its description stays beside `Off` /
  `On`** (agreed). A plain label among the governed rows, or directly above
  the first of them, goes under `On` only when it sits in the SAME LIST as
  them -- `Info to show` and `Style` head Watermark's options; Log's
  "Add an option to record in Log format..." is in a description block of
  its own and stays at the switch's level. Decided by layout, not wording.
* **Choices are named by the phone's own descriptions** (agreed):
  `Selected, Alignment Left`, `Alignment Center` -- not shortened.
* **A replay passes through a switch** by setting it, not by looking for a
  control called `On`: `Watermark > On > Edit` is replayed by opening
  Watermark and turning its switch on.
* **A press that opens a panel on the same screen** (agreed, Quick controls):
  options are listed under the control and never selected (Flash, Timer,
  Ratio, Resolution); a dial is listed by its title, its value and its Close
  (Exposure, Face) and never moved. The walk leaves by the panel's Close, and
  is home again only when the control it pressed is back in view.
* **A selection group is options**: clickable siblings of one class and id
  with exactly one marked `selected` (Flash Off / Auto / On, timers, ratios,
  zoom lenses). Pressing one changes the setting, so none is pressed.
* **A press that only shows a caption** (agreed): the caption is a `[Title]`
  row right after the control, at its depth (`Not available with blur
  effect.` after Lighting), and the control is pressed once more -- Night shot
  is a toggle and nothing in the layout says so.
* **Controls that switch the whole app** (`walk_separately` in the profile,
  agreed: Switch to front camera) are listed, never pressed in the main walk;
  what they lead to is walked as a pass of its own.
* **A control carried over from the screen above** (the top bar's Flash on the
  Quick controls panel) is not listed again when the screen has its own copy.
  A screen counts as reached only when one of its OWN controls is in view.
* **A replayed mode is confirmed** by the mode strip's own description
  ("Portrait, Mode"); after a reset a first-launch tip took the first tap.
* **A carousel that selects** (agreed, Filters): a horizontal list that
  announces its selection in its own description ("Original Filter"). It is
  never swept -- swiping selects. Each item is selected in turn; pressed again
  it opens its editor, whose adjustments (Strength, Contrast, ...) are listed,
  Reset listed and refused, left by Close; what selecting brings ("View
  original") is listed under the item. An item that does not select (Custom
  filters) is handled like any press. The item selected at the start is
  selected again at the end. Title and announcement rows are not repeated.
* **A toggle that shows its own state** (agreed, Face in PHOTO): a control
  whose label ends with the text of its own child ("Face retouching: Off"
  holding "Off"). Handled as a switch: its name, then `Off` / `On`, what
  turning it on shows under `On`; turned back at once. Its state text is not
  a row of its own.
* **A panel is told from a new screen** also by what it leaves in place: if
  at least half of the screen's carried-over controls (top bar, shutter)
  are still there after the press, it opened a panel. Face's panel hid the
  chip row and the mode strip and was walked as a screen, its items pressed.
* **An unlabelled control inside a named container** is named by the
  container and not listed (`beauty_list_item_button` in "Smoothness").
  In a panel every displayed value is kept, even when values repeat.
* **A row of items cut off at its edge** (PHOTO's Quick controls: Filters and
  Face lie past Motion photo) is dragged along to see the rest, and back.
* **Rows that leave the app** for somewhere outside its menu tree are listed,
  not opened (`leaves_app` in the profile; agreed: Download filters).
* **Modes under MORE** (agreed): each tile is a node (MORE > PRO > ...), left
  by the mode's own on-screen Back. A tile that opens another app (EXPERT RAW
  -> Galaxy Store, Bixby Vision) has that app's first screen listed one level,
  nothing in it pressed or answered. MORE > Edit is `list_only` in the profile.
* **Another app is listed once** per control (`--revisits` other than full):
  the Gallery from every mode is "opens ...gallery3d; listed under ...".
* **A screen reached again by another route** (Settings from every mode):
  `--revisits full|top|note`, default `top` -- its rows listed again, rows it
  already had where it was walked before not reopened. Only a near-identical
  screen (>= 90% the same) counts; Quick controls differ per mode and are
  always walked.
* **A control that relabels itself** when pressed (FOOD_BLUR_EFFECT_ON ->
  _OFF, same id and place) is a toggle: the new state recorded, pressed back.
* **A control named as a screen on the current path** is a title, not
  pressed (FOOD's `FOOD` leaves the mode).
* **Getting back never uses a control carried over from above**: FOOD's own
  `Back` stays visible over its Quick controls' panels and leads out of FOOD;
  nor does a panel's "close" use it.
* **Replay confirms each step** against the screen the walk recorded there.
* **Panel or screen** is decided by what the press brought: if it brought
  controls that are clickable in their own right and need walking (Quick
  controls' chips), it opened a screen; if it brought only options, back
  controls, readings or a state toggle (Flash, Timer, Mic, a dial, Face),
  or it landed back on the screen above (PRO VIDEO's Video size), it is a
  panel listed in place. Options are recognised among siblings or cousins
  (one wrapper each, as Mic's sources) with exactly one `selected`.
* **A panel is closed** by its own close control, else by pressing the chip
  that opened it again, else by the return ladder -- and is only "home" when
  most of the screen's own controls are back in view.
* **Returning from a mode** that its own Back leaves for PHOTO: the walk
  walks the path again from where it stands (MORE, the tile) before it ever
  uses the system Back, which closes the camera from a mode screen.
* **A press that clears things before showing them** (Filters clears the top
  bar, then slides its carousel in) is read again a moment later.
* **Separate passes** (agreed, the front camera): after the main walk, each
  `walk_separately` control visible on the launch screen opens a pass. The
  app is relaunched, the control pressed, and everything walked again under
  a depth-2 node named after it (`Switch to front camera` > PORTRAIT / PHOTO
  / VIDEO / MORE at depth 3). The mode strip is forgotten first so the new
  state's strip is entered again. A screen in the pass that has left the
  pass's state -- the starting control is visible again, or it and its
  parent are the main walk's very screens (FOOD, PANORAMA switch to the rear
  camera) -- is a leaf with a note. Settings follows `--revisits`. The app is
  relaunched at the end, which with the usual reset restores the rear camera.
* **First launches** (agreed, step 7): `first_launches` in the profile (3 for
  the camera). The app is cleared and launched that many times; each launch's
  self-closing tips and pop-ups are written under "First launches (app data
  cleared)" > "Launch n", before the modes. Every answer to every pop-up is
  followed (clear, launch, answer again each time), explored level by level so
  that an answer that changes the PHONE comes last (Google Play services'
  Location Accuracy "Turn on", pressed as agreed -- it stays on until switched
  off by hand). A pop-up met again is written once ("the same pop-up as
  above"), going back to one above is "returns to". The main walk then runs in
  the state `launch_answers` sets (agreed: Turn on, While using the app, No
  thanks), and every wiped relaunch is brought back to it.
* **Suggestions** the app shows by conditions (`suggestions` in the profile:
  "Use Night mode", the Quick controls tip) are listed, never pressed.
* **A row of buttons is a choice only when it reads as one**: named by
  descriptions alone (alignment icons) or with exactly one selected
  (Natural / Warm). A pop-up's Cancel / Turn on are answers.
* **A tap lands on what is seen.** A control is tapped in the middle of the
  part of it that shows: clipped to its list's viewport, clear of the status
  and navigation bars, and clear of a band pinned over the list (a toolbar).
  A row that is on screen but covered is nudged into the clear with short
  drags. Tapping a covered `Video format` hit the toolbar, which went Back.
* **A restore is confirmed.** The switch is tapped once and the walk waits
  for it to *report* the new state. It is found by its own row, scrolling
  until the switch itself is in view and not under the status bar (a sticky
  switch bar pins itself half under it once the list scrolls).
* **A press is given time to land.** After a tap the walker waits (up to 3 s)
  for the screen's labels to change before deciding the press did nothing. A
  screen still sliding in (Pro mode picture format) or a pop-up list still
  fading in (Watermark's Font list) was otherwise written off.
* **A segmented choice** — two or more sibling views of one class, all
  clickable, all leaves, each with its own description (Watermark's
  Alignment Left / Center / Right) — is a set of **options**: listed, never
  pressed. Which one is chosen is only in the accessibility text, so there is
  no attribute to read; pressing one would change the setting. Views that
  hold children (quick-setting chips, lens buttons) are not caught by this.
* **A text of several paragraphs** is one row per paragraph, as the sheet
  splits Log's description. Only plain text is split; a control's label stays
  whole, because that label is how it is found again.
* **Container rows** -- a clickable container with no text whose children
  carry the labels (`main_switch_layout`, `spinner_layout`) -- are not rows;
  the children name them. A switch's own state text is not a row either.
* **Summary lines** are rows at the setting's own depth, directly after it
  (I20 Shot suggestions, I21 its description, I22 its switch).
* **A dialog** is opened, its title and buttons listed as rows, and left by
  `Cancel`. A destructive button (`Reset`) is listed, never pressed.
* **A row that opens another app** (Camera Assistant, Permissions) is walked
  one level into that app — its first screen recorded as the row's children
  — and then the walk returns to the camera.

## Starting state and the first-launch pop-ups

Every exploration starts from a wiped app: `pm clear` (data and cache) plus
`pm reset-permissions`, in `device_driver.wipe_app`. Location and Bluetooth
come back ungranted, so their prompts return. Camera, Microphone, Photos and
Notifications are granted to this preinstalled app by default and are never
prompted for, on a new phone or here.

**The Location prompt appears on the THIRD launch after a wipe**, not the
first (measured: launches 1 and 2 open the viewfinder). Its chain:

```
Turn on Location tags?                       Cancel | Turn on
  Allow Camera to access this device's location?   (permissioncontroller)
    Precise | Approximate
    While using the app | Only this time | Don't allow
```

Each answer is one-shot -- once given, Android never offers the dialog
again. **Every branch is documented**: before each one the app is wiped and
launched three times, then that branch is walked. About 3-4 minutes.

The rest of the walk then starts with **Location granted** (Turn on > While
using the app), so location-dependent settings are in their enabled state.

## Order

Pre-order, exactly as a tester writes the sheet: a row, then everything it
opens, then the next row. Rows are emitted at the moment they are reached.

## Quick controls and Settings are walked from every mode

Both menus are dynamic (confirmed by the sheet's author): what they hold
depends on the mode they are opened from. Resolution is not in PORTRAIT's
Quick controls; in night mode Flash, Face, Resolution, Motion photo and
Filters are not available; a setting disabled in the current mode shows
"Not available with the current mode or front camera." in place of its
summary. So both are walked under every mode, and each records exactly what
that mode shows -- no list of expected items exists anywhere. A To_Be row
missing in one mode is a mode difference, not a walker miss.

## Repetition

A control reachable from two places is listed under both (Flash on the
viewfinder *and* under Quick controls). Loops are stopped by the cycle
guards, not by suppressing repeats.

## Measured, not assumed

Observed with `inspect_screen.py --probe` on `PORTRAIT > Quick controls >
Go to Settings`:

| row | pressing it |
|---|---|
| Scanning, Photo enhancer, Custom filters, Watermark | opens a screen |
| Shot suggestions | nothing visible changes — the press flips the switch |
| Watermark | opens a screen, though the row also carries a switch |
| Photo format | opens its options |

`Shot suggestions` and `Watermark` look the same in the dump and behave
differently. That is why switch rows are detected by observation rather than
by a rule written in advance.

## First review (2026-10-15) -- rules applied across the whole tree

The sheet's author reviewed run 2026-10-15 05:07 in sheet `MenuTree_To_Be`,
column R "Review Comments"; each comment is a rule for every screen.

* **A widget sits beside its item**, on the same row in the next column: a
  radio or checkbox beside its option ("Balanced | radio (On/Off)"), a
  switch beside the last line of its row ("Automatically adjust ... |
  switch_widget (On/Off)", "Scan text | switch_widget (On/Off)").
  A switch found by its description ("Document scan", "Watermark") is
  written after its item's whole subtree; it is matched back to the item
  with the same name at the same depth on the same screen and goes beside
  that item's last line. `build_menutree.py --skip-walk` re-applies these
  layout rules to an existing walk (the walked rows are kept as
  `menutree_rows.walked.json`).
* **A row's current value goes directly under its item**, on its own row,
  before the options pressing it brings ("Scan resolution / Default / High").
* **A switch whose Off / On rows were written** is not repeated by its own
  text and widget ("On", "On (On/Off)").
* **A control named by its description** whose text is its state is written
  "name | state" ("Resolution | BACK_CAMERA_PRO_PICTURE_SIZE_NORMAL"); a new
  state after pressing is a row of its own beneath it. Options keep their
  own labels.
* **Only what is new**: under any screen or panel opened from another, the
  controls still visible from the screen behind are not repeated (PRO >
  Exposure monitor, Quick controls). Back controls stay on every screen.
* **Live readings** (values that change while nothing is pressed) belong to
  the screen that first showed them. While the screen behind is still
  showing, a view of it that READS differently is still its own: a value
  inside a control carried over unchanged (ISO's "800" inside the ISO dial),
  or a view in the same place with the same id and description (Flash
  reading "BACK_FLASH_OFF"). The light changes between two screens, so PRO's
  bar read "3200", "1/90s", "3200K" over its dials (second review).
* **PRO dials** list their controls and the scale values seen (agreed); the
  autofocus modes are pressed, recorded and put back (agreed). On the phone
  a dial in Auto exposes no scale marks: its panel adds About selected
  setting, the Auto button, the dial's level and Close -- written as
  `ISO / 800 (summary) / About selected setting / Auto ISO (option) /
  800 (on a slider) / Close`.
* **A slider is never swiped or tapped.** It is a value, not a list: the
  sweep for rows below the fold picks a scrollable container, and on a
  dial panel the only one is the dial (a SeekBar) -- 74 sweeps turned ISO
  manual and SPEED to 30 s. A label drawn on a slider with nothing
  pressable around it (the dial's level) is listed `on a slider -- listed,
  not pressed`: tapping it set the value (focus went to MF).
* **A row that names itself by its description holds its value as text**:
  SPEED's "1/30s" is its summary, not its title, and is not pressed.
* **Back from a screen means what the press opened has gone again**, not only
  that the screen's own controls are visible -- a panel adds a few controls
  to a screen that is still fully showing.
* A guarded row is opened only from a ROW of a list -- one that fills its
  list's width (Settings' Reset settings). A chip in a strip is not a row:
  PRO's bar of dials scrolls sideways, and its "Reset" (shown once any dial
  is manual; it resets them all) is listed, never pressed.
* **PRO manual mode is recorded** (agreed 2026-10-16). A dial's Auto button
  is pressed, the state it switches to is written under it with what came
  with it, and it is pressed back:
  `Auto ISO / Manual ISO (state after pressing -- put back) / Reset (refused)`.
  SPEED's manual mode also turns the flash off (`BACK_FLASH_OFF`). FOCUS's
  "Auto Focus" opens the focus areas, CENTER / MULTI / MANUAL, listed as
  options (not pressed). EV has no Auto button.
* **Auto framing is a toggle with a caption** (agreed 2026-10-16), like
  Night shot: its message ("Auto framing On / The zoom will automatically
  change...") is written as a caption, and it is pressed again. Controls the
  press only redraws (same id, same name) are not something it brought.
* **A label drawn on a control with the same label is that control**, even
  when it is a sibling and not a child: the focus areas' names sit inside
  their buttons' boxes, and pressing the name pressed the button.
* **A state change is put back only when its new label has gone**, not
  merely when the old label is visible somewhere. Super steady's Quick
  controls chip keeps reading _OFF while its top-bar copy flips. The pressed
  control's own new label is taken before any other control's.
* **A control's text that is dynamic stays as the phone shows it**: the
  Watermark text field is "Customize text | Edit" (confirmed in the second
  review, 2026-10-16).

## Samsung Internet and long press (agreed 2026-10-19)

* **Long press** is walked like any press, for every app, from the
  `long-clickable` flag: a "(long press)" row after the control, and what it
  opens is walked. Not text fields, options, tabs, or what the walk would not
  press anyway; in a list only the first item. A long press that acts at once
  (Tabs opens a new tab) is noted "acts at once" and not undone with a tap.
* **First-launch screens may be full screens**: a screen that offers one of
  the profile's `launch_answers` is a first-launch screen (the Internet app's
  welcome pages). `do_not_press` holds in the first-launch recorder too ("Set
  as default").
* **A tab strip is a ROW** of Buttons sharing one id; a stacked list (Tools)
  is a menu.
* **Icon buttons named by description are a choice only when the names share
  a word** (Alignment Left / Center / Right), not a toolbar.
* **Profile `option_words`**: a choice that states itself only in its spoken
  description ("Not selected, List, Radio button") is an option; a label
  inside an option is part of it.
* **Profile `list_only` may name a resource id** (the address bar, whose
  label is whatever URL shows) or a long press ("Home (long press)").
* **Camera MORE tiles: long press is list-only** (2026-10-20). It opens the
  same edit mode as MORE > Edit ("Touch and hold to move modes.", Save); a
  full run pressed Save and the tiles there. Each "<tile> (long press)" is in
  the camera profile's `list_only`, like Edit.
* **The keyboard is never typed on**: a screen with a focused text field is
  not swept, and no tap lands inside the keyboard.
* **A press that switches to the app's main activity from another** (a
  bookmark or History entry loading its page) is content: "opens the app's
  main screen", not walked. Not inside a separate pass.
* **Separate passes start where their control was listed** (Secret mode is
  under Tabs).
* **Refresh -> Stop**: one new control in the pressed control's place is its
  new face, not a new screen.
* **Edit / Move / Create folder in a bookmark's selection mode** are pressed
  through (agreed); the app is cleared at the start of every run.

## Output and in-place options (2026-10-20)

* **Options a chip opens in place are closed after listing** -- the chip is
  pressed again, only while they still show. One phone kept PORTRAIT's Flash
  options open, and Quick controls (pressed next) listed them as its own.
* **Every file of a run is named after the run** (`<run>_menutree.xlsx`, ...),
  so two MenuTrees can be open in Excel together.
* **MenuTree layout**: A-D width 8, E-R 16, S-V 4, all cells wrap.
* **Object repository**: every element a row names, with all its locators,
  in `<run>_object_repository.json`, the ObjectRepository sheet and
  `<run>_page_objects.py`. Rows name it in the last column, "Object"; UVTA
  cases keep literal selectors and are preceded by `# object: <name>`.
* **Summary sheet** (first in the workbook): what the tree was generated
  on -- date, phone (name, model, serial), software (Android, API, One UI,
  security patch), build (build number, AP, CP, CSC, fingerprint), hardware
  (revision, SoC, bootloader), the app (name, package, version, code) -- and
  the tree's size: rows, deepest depth, rows per depth and kind, and how the
  walk went (duration, returns, switches put back). Read over adb, read-only,
  before the walk, kept in `<run>_run_info.json`. The app name is the
  profile's `app_name` (the phone gives only a resource number). A
  re-export of an older run reads the phone then and says so on the sheet.
* **A separate pass starts only from where the main walk lists its control**
  (2026-10-20). Inside the front-camera pass, "Switch to rear camera" was
  recorded and its pass then ran inside the front camera; a control seen
  inside a pass is not recorded.
* **Camera: the preview's "Scan" and "Scan text" buttons are suggestions**
  (listed, never pressed; 2026-10-20). They show only when the camera sees a
  document; pressing "Scan" opened the document scanner and VIDEO was never
  walked. Named by resource id (`scan_layout`, `text_scan_button`):
  Settings > Scanning has a real "Scan text" switch, which is pressed as
  before. `suggestions` may name a label or a resource id.
* **The front camera may open in PORTRAIT** (2026-10-22), with MORE off the
  edge of its mode strip; the strip read again after VIDEO finds it.
* **No tap lands on a protected control by accident** (2026-10-20). Runs
  took photos, each during a launch or relaunch: a prompt drawn over the
  shutter was pressed until it went, and a tap read from a dump just before
  it closed landed on the shutter. A tap on a point where a protected
  control (do_not_press, action guard) is -- or was last seen, since a
  system prompt's dump does not hold the app beneath it -- is checked
  against a fresh dump: the target must still be there with nothing
  protected drawn over it, or the tap is not made ("tap ... not made" in
  the log, `taps_refused_over_protected` in the stats).
* **Another app starting is believed from the dump** (2026-10-20): when the
  dump shows another app but the system still reports ours, it is asked
  again after 0.5 s. MORE > EXPERT RAW's Galaxy Store page was lost that way.
* **A tab strip is read again after its tabs are walked** (2026-10-20); a
  tab of the same strip not seen the first time is walked too. The front
  camera's strip was read mid-draw and MORE was never walked. The strip
  read is logged ("tab strip at ...").
