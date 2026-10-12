"""Per-app knowledge, kept as data beside the app it describes.

The walker is app-agnostic: it must work on any Android app without edits.
Whatever is genuinely specific to one app -- a control that must never be
pressed because it has a side effect (a camera shutter saves a photo on every
tap), or the alias file mapping a sheet's wording to that app's constants --
lives in `profiles/<package>.json`, not in code.

A missing profile is normal and means "no app-specific knowledge": the walk
runs on structure and observation alone.

    {
      "do_not_press": ["Take picture", "Start recording"],
      "aliases": "aliases/samsung_camera.json"
    }
"""
import json
from pathlib import Path
from typing import Dict

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


def load_profile(package: str) -> Dict:
    path = PROFILE_DIR / f"{package}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}
