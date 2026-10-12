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

* **A switch that reveals content** gets `Off` and `On` rows beneath the
  setting, and everything that appears when ON goes under `On` — as the
  To_Be sheet's Watermark (J52 Off, J53 On, K54 Info to show …).
* **`Off` / `On`** sit at the switch's own level; what the switch governs goes
  one level under `On`.
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
