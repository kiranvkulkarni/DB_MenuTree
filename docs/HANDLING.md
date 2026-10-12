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
| **action** | shutter, record, camera switch, gallery | list; do not perform |
| **back** | Navigate up / Close / Back | the route back; not a row |
| **header** | text with no clickable row around it | list |
| **system chrome** | status bar, navigation bar, Edge panels | ignore |

## Sheet shape, as the To_Be sheet writes it

* **A switch that reveals content** gets `Off` and `On` rows beneath the
  setting, and everything that appears when ON goes under `On` — as the
  To_Be sheet's Watermark (J52 Off, J53 On, K54 Info to show …).
* **Summary lines** are rows at the setting's own depth, directly after it
  (I20 Shot suggestions, I21 its description, I22 its switch).
* **A dialog** is opened, its title and buttons listed as rows, and left by
  `Cancel`. A destructive button (`Reset`) is listed, never pressed.
* **A row that opens another app** (Camera Assistant, Permissions) is walked
  one level into that app — its first screen recorded as the row's children
  — and then the walk returns to the camera.

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
