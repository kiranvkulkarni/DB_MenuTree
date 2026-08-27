"""Offline checks for the emitted UVTA suite.

Two properties matter, and both were wrong at some point:

* **Every control is addressed the way it is actually addressable.**
  A row's label is whichever of text or content-desc was non-empty, so
  emitting `text "<label>"` for everything produced selectors that cannot
  match for any icon. The suite looked complete and would have failed on
  execution.

* **Every verify proves the click before it landed.** The emitter used to
  click a step and then assert that same step still existed. A menu item that
  opens a submenu is usually replaced by it, so that assertion passed
  vacuously when the item happened to remain and failed spuriously when it
  did not. Neither outcome said anything about whether the click worked.

    python tests/test_uvta.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.generator.tree_uvta import emit  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def steps_for(rows, name_fragment):
    for case in emit(rows, "com.x"):
        if name_fragment in case.name:
            return case.steps
    return []


def main() -> int:
    ok = True

    rows = [
        {"label": "Quick settings", "raw_label": "Quick settings", "kind": "button",
         "depth": 2, "path": [], "path_selectors": [], "interactive": True,
         "selector_kind": "id", "selector_value": "quick_setting_entry_button"},
        {"label": "Back key icon", "raw_label": "Back key icon", "kind": "button",
         "depth": 3, "path": ["Quick settings"],
         "path_selectors": [["id", "quick_setting_entry_button"]],
         "interactive": True, "selector_kind": "desc", "selector_value": "Navigate up"},
        {"label": "Flash", "raw_label": "Flash", "kind": "button", "depth": 4,
         "path": ["Quick settings", "Go to Settings"],
         "path_selectors": [["id", "quick_setting_entry_button"],
                            ["text", "Go to Settings"]],
         "interactive": True, "selector_kind": "text", "selector_value": "Flash"},
    ]

    print("selector priority reaches the emitted suite")
    deep = steps_for(rows, "Flash")
    ok &= check("an id-only control is addressed by id",
                'click id "quick_setting_entry_button"' in deep)
    ok &= check("a text control is addressed by text",
                'verify text "Flash" exists' in deep)
    icon = steps_for(rows, "Back_key_icon")
    ok &= check("an icon is addressed by its description, not its sheet label",
                'verify desc "Navigate up" exists' in icon,
                "text \"Back key icon\" would never match")
    ok &= check("the icon's sheet label never becomes a selector",
                not any('"Back key icon"' in s for s in icon))

    print("\nevery verify proves the click before it")
    # launch, click A, verify B, click B, verify row
    ok &= check("a two-step path emits click/verify in a chain",
                deep == ['launch "com.x"',
                         'click id "quick_setting_entry_button"',
                         'verify text "Go to Settings" exists',
                         'click text "Go to Settings"',
                         'verify text "Flash" exists'],
                " | ".join(deep))
    for i, step in enumerate(deep):
        if step.startswith("verify") and i > 0:
            ok &= check(f"verify at step {i} follows a click",
                        deep[i - 1].startswith("click") or deep[i - 1].startswith("launch"))
    ok &= check("no verify asserts the element just clicked",
                not any(deep[i - 1] == step.replace("verify ", "click ").replace(" exists", "")
                        for i, step in enumerate(deep) if step.startswith("verify") and i > 0))

    print("\na row on the entry screen needs no click")
    top = steps_for(rows, "Verify_Quick_settings")
    ok &= check("launch then assert, nothing clicked",
                top == ['launch "com.x"',
                        'verify id "quick_setting_entry_button" exists'],
                " | ".join(top))

    print("\nrows from an older file without path_selectors still emit")
    legacy = [{"label": "Flash", "raw_label": "Flash", "kind": "button", "depth": 3,
               "path": ["Quick settings"], "interactive": True}]
    steps = steps_for(legacy, "Flash")
    ok &= check("falls back to text for both path and row",
                steps == ['launch "com.x"',
                          'click text "Quick settings"',
                          'verify text "Flash" exists'],
                " | ".join(steps))

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
