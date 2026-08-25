"""Offline checks for matching hand-written spec labels to screen elements.

The depth columns of the MenuTree workbook are not selectors. They are how a
manual test engineer described each control, in their own English, while
looking at the phone -- so the sheet says "Flash icon" where the screen says
"Flash", and "Priorize quality" where the screen says "Prioritize quality".

Two failure modes matter, and they are not symmetric:

* **A false negative manufactures a defect.** Reporting Fail on a control
  that is present and working is the worst thing a release gate can do, so
  matching has to tolerate wording drift.
* **A false positive hides one.** Matching "Photo" to "Video" would report a
  pass for a screen nobody checked, so tolerance must not become blindness.

The cases below are drawn from real sheet wording.

    python tests/test_matching.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.verify.matching import (  # noqa: E402
    CONFIDENT,
    REVIEW,
    best_match,
    from_resource_id,
    stem,
    load_aliases,
    proposed_aliases,
    score,
)


class FakeElement:
    def __init__(self, label, resource_id=None, kind="item"):
        self.label = label
        self.resource_id = resource_id
        self.kind = kind


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def main() -> int:
    ok = True

    print("wording drift must still match (a false Fail invents a defect)")
    should_match = [
        ("Flash icon", "Flash", "descriptive noun dropped"),
        ("Settings icon", "Settings", "descriptive noun dropped"),
        ("Back key  icon", "Back", "double space and two noise words"),
        ("Priorize quality", "Prioritize quality", "typo in the sheet"),
        ("High efficiency pitures", "High efficiency pictures", "typo"),
        ("Watermark(On/Off)", "Watermark", "annotation the sheet appends"),
        ("Motion Photos", "Motion photo", "singular vs plural"),
        ("Shot Suggestion", "Shot suggestions", "singular vs plural"),
        ("Location tags ...recorded.", "Location tags will be recorded.",
         "author elided the middle"),
        ("while using this app", "While using the app", "paraphrase"),
    ]
    for spec, screen, why in should_match:
        value, reason = score(spec, screen)
        ok &= check(f"{why}: {spec!r} -> {screen!r}", value >= REVIEW,
                    f"{value:.2f} {reason}")

    print("\ndifferent controls must NOT match (a false Pass hides a defect)")
    should_not = [
        ("Photo", "Video"),
        ("12MP", "50MP"),
        ("Flash icon", "Timer"),
        ("Scan Text", "Scan QR codes"),
        ("Front Camera", "Rear Camera"),
        ("ON", "OFF"),
    ]
    for spec, screen in should_not:
        value, reason = score(spec, screen)
        ok &= check(f"{spec!r} is not {screen!r}", value < REVIEW,
                    f"{value:.2f} {reason}")

    print("\nsingular/plural must not keep a match apart")
    # One character sank the whole Settings sheet: the sheet says "Quick
    # settings", the control's id is quick_setting_entry_button, and every
    # row lived under that first step -- 80 of 88 rows reported Fail against
    # controls that were present and working.
    ok &= check("plural spec vs singular resource id",
                score("Quick settings", "quick setting entry button")[0] >= 0.8,
                f"{score('Quick settings', 'quick setting entry button')[0]:.2f}")
    ok &= check("singular spec vs plural screen text",
                score("Filter", "Filters")[0] >= REVIEW)
    # Test stem() directly. Going through score() would prove nothing here:
    # "ON"/"ONS" already match at 0.88 by containment, which has nothing to
    # do with stemming.
    ok &= check("stemming folds a real plural", stem("settings") == "setting")
    ok &= check("stemming leaves short words alone", stem("ons") == "ons",
                "'on' must never become 'o'")
    ok &= check("stemming leaves a double-s alone", stem("gloss") == "gloss")
    ok &= check("stemming leaves a non-plural alone", stem("focus") == "focus")
    ok &= check("stemming does not rescue genuinely different words",
                score("Quick settings", "Quick controls")[0] < REVIEW,
                "settings/controls differ in meaning, not in number")

    print("\nresource ids are developer English, often closer than the visible text")
    ok &= check("id is split into words",
                from_resource_id("com.x:id/flash_auto_button") == "flash auto button")
    ok &= check("camelCase is split",
                from_resource_id("com.x:id/flashAutoButton") == "flash auto button")
    ok &= check("no id is not a crash", from_resource_id(None) == "")

    print("\npicking the best element on a screen")
    screen = [
        FakeElement("Video"),
        FakeElement("Prioritize quality"),
        FakeElement("", resource_id="com.x:id/shutter_button"),
        FakeElement("Photo"),
    ]
    m = best_match("Priorize quality", screen)
    ok &= check("typo resolves to the right element",
                m is not None and m.element.label == "Prioritize quality",
                f"{m.score:.2f} {m.why}" if m else "no match")

    m = best_match("Shutter button", screen)
    ok &= check("an icon with no text is found by resource id",
                m is not None and m.matched_on == "resource-id",
                f"{m.score:.2f} via {m.matched_on}" if m else "no match")

    m = best_match("Bluetooth pairing", screen)
    ok &= check("something genuinely absent scores below the threshold",
                m is None or m.score < REVIEW,
                f"{m.score:.2f}" if m else "no match")

    ok &= check("empty spec label matches nothing", best_match("", screen) is None)
    ok &= check("empty screen matches nothing", best_match("Photo", []) is None)

    print("\na confirmed alias beats any guess")
    aliases = {"Back key icon": "Navigate up"}
    screen2 = [FakeElement("Navigate up"), FakeElement("Back")]
    m = best_match("Back key icon", screen2, aliases)
    ok &= check("alias wins over the closer-looking text",
                m is not None and m.element.label == "Navigate up"
                and m.score == 1.0,
                f"{m.element.label!r}" if m else "no match")
    m = best_match("Back key icon", screen2)
    ok &= check("without the alias it picks the textual match",
                m is not None and m.element.label == "Back",
                f"{m.element.label!r}" if m else "no match")

    print("\nreview file round trip")
    log = [
        {"sheet_row": "Modes!12", "spec_label": "Priorize quality",
         "on_screen": "Prioritize quality", "score": 0.75, "why": "1 word in common",
         "matched_on": "label"},
        {"sheet_row": "Modes!13", "spec_label": "Watermark",
         "on_screen": "Watermark", "score": 1.0, "why": "exact",
         "matched_on": "label"},
    ]
    review = proposed_aliases(log)
    ok &= check("exact matches are not proposed as aliases",
                "Watermark" not in review["aliases"])
    ok &= check("inexact matches are proposed",
                "Priorize quality" in review["aliases"])

    path = Path(tempfile.mkdtemp()) / "alias_review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    loaded = load_aliases(path)
    ok &= check("a weak match is NOT auto-confirmed",
                "Priorize quality" not in loaded,
                "score 0.75 is below CONFIDENT, so a human must approve it")

    review["aliases"]["Priorize quality"]["confirmed"] = True
    path.write_text(json.dumps(review), encoding="utf-8")
    ok &= check("once confirmed, it loads",
                load_aliases(path).get("Priorize quality") == "Prioritize quality")

    plain = Path(tempfile.mkdtemp()) / "plain.json"
    plain.write_text(json.dumps({"A": "B"}), encoding="utf-8")
    ok &= check("a plain {spec: screen} map also loads",
                load_aliases(plain).get("A") == "B")

    print(f"\nthresholds: confident >= {CONFIDENT}, review >= {REVIEW}")
    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
