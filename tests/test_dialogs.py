"""Offline checks for getting out of a modal that is in the way.

A blocking dialog is the most expensive failure this tool has, because it
does not look like a failure. The Samsung camera raises "Turn on Location
tags?" partway through a run; until something presses a button, every tap
lands on the scrim and the walk records control after control as
`unreachable` while the app sits there perfectly healthy. One observed run
spent its whole 3000s budget that way. A crash would have been kinder.

Two properties are non-negotiable:

* **The escape declines.** A run that clears prompts by pressing the
  affirmative branch turns location tagging on, grants permissions and
  accepts terms -- it changes the device it is supposed to be measuring.
* **The walker and the verifier make the same choice.** If discovery
  declines a prompt and verification accepts it, the two tools are looking
  at different devices and their numbers cannot be compared.

    python tests/test_dialogs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.hierarchy import (  # noqa: E402
    ACKNOWLEDGE_LABELS,
    DECLINE_LABELS,
    pick_dismissal,
)
from src.crawler.element_tree import ElementTreeWalker  # noqa: E402
from src.verify.verifier import MenuTreeVerifier  # noqa: E402


class FakeElement:
    def __init__(self, label):
        self.label = label


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def main() -> int:
    ok = True

    print("the real dialog, as photographed on an S25 FE mid-run")
    # "Turn on Location tags?  Location tags add geographical location data
    #  to your pictures and videos ...  Learn more   Cancel   Turn on"
    dialog = [FakeElement("Turn on Location tags?"),
              FakeElement("Location tags add geographical location data to "
                          "your pictures and videos so you can search for "
                          "and sort them based on where they were taken."),
              FakeElement("Learn more"),
              FakeElement("Cancel"),
              FakeElement("Turn on")]
    choice = pick_dismissal(DECLINE_LABELS, dialog)
    ok &= check("presses Cancel", choice is not None and choice.label == "Cancel",
                choice.label if choice else "nothing")
    ok &= check("never presses Turn on",
                choice is None or choice.label != "Turn on",
                "pressing it would enable location tagging on the device")
    ok &= check("never presses Learn more",
                choice is None or choice.label != "Learn more",
                "that leaves the app for a browser")

    print("\npriority is the label list, not the order on screen")
    # Cancel and Turn on sit side by side; document order picks whichever the
    # layout happened to place first, which is locale-dependent.
    reversed_layout = [FakeElement("Turn on"), FakeElement("Cancel")]
    choice = pick_dismissal(DECLINE_LABELS, reversed_layout)
    ok &= check("affirmative listed first is still not chosen",
                choice is not None and choice.label == "Cancel",
                choice.label if choice else "nothing")

    print("\nno decline label is affirmative")
    affirmative = ("ok", "allow", "turn on", "yes", "accept", "agree",
                   "continue", "enable", "confirm", "got it")
    for word in DECLINE_LABELS:
        ok &= check(f"{word!r} declines", word not in affirmative)
    ok &= check("acknowledgements are kept separate from declines",
                not set(DECLINE_LABELS) & set(ACKNOWLEDGE_LABELS))

    print("\nacknowledgement is a last resort, not an alternative")
    # "Location tags and sharing ... OK" offered no non-committal branch and
    # stranded 53 of 60 rows while the run relaunched 52 times into it.
    info = [FakeElement("Location tags and sharing"), FakeElement("OK")]
    ok &= check("no decline available on an informational dialog",
                pick_dismissal(DECLINE_LABELS, info) is None)
    ok &= check("acknowledgement then clears it",
                (pick_dismissal(ACKNOWLEDGE_LABELS, info) or FakeElement("")).label == "OK")

    print("\nthe action guard still vetoes a press")
    risky = [FakeElement("Cancel"), FakeElement("OK")]
    ok &= check("a blocked decline is skipped, not pressed",
                pick_dismissal(DECLINE_LABELS, risky,
                               lambda text: "destructive" if text == "Cancel" else "") is None,
                "and the caller falls through to BACK")
    ok &= check("an unblocked label is still found",
                (pick_dismissal(ACKNOWLEDGE_LABELS, risky, lambda text: "") or
                 FakeElement("")).label == "OK")

    print("\nnothing to press on a screen that is not a dialog")
    ok &= check("no false press on the viewfinder",
                pick_dismissal(DECLINE_LABELS,
                               [FakeElement("Flash"), FakeElement("PHOTO")]) is None)

    print("\nboth tools make the same choice")
    ok &= check("verifier declines from the shared list",
                tuple(MenuTreeVerifier.ENTRY_DISMISS) == tuple(DECLINE_LABELS))
    ok &= check("verifier acknowledges from the shared list",
                tuple(MenuTreeVerifier.ENTRY_ACKNOWLEDGE) == tuple(ACKNOWLEDGE_LABELS))
    ok &= check("the walker has an escape at all",
                hasattr(ElementTreeWalker, "_clear_blocking_dialog"),
                "without it a modal is recorded as the whole app being unreachable")

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
