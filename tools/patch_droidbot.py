"""Patch the installed DroidBot for modern dependency versions.

Upstream honeynet/droidbot is stale in ways that stop it importing at all on a
current dependency set. This script patches the *installed* package in
site-packages, so it must be re-run after any `pip install droidbot`.

It is idempotent: running it twice is safe, and it reports what it changed.

Patches applied
---------------
1. androguard 4.x moved `androguard.core.bytecodes.apk` -> `androguard.core.apk`.
   DroidBot still uses the 3.x path and crashes on startup with
   ModuleNotFoundError. Replaced with a try/except that supports both.

Run:  python tools/patch_droidbot.py
"""
import sys
from pathlib import Path

PATCHES = [
    {
        "name": "androguard-4.x-import",
        "file": "app.py",
        "old": "        from androguard.core.bytecodes.apk import APK\n",
        "new": (
            "        try:\n"
            "            from androguard.core.apk import APK  # androguard >= 4.0\n"
            "        except ImportError:\n"
            "            from androguard.core.bytecodes.apk import APK  # androguard 3.x\n"
        ),
        "already": "from androguard.core.apk import APK",
    },
]


def find_droidbot() -> Path:
    try:
        import droidbot
    except ImportError:
        sys.exit(
            "droidbot is not installed in this interpreter.\n"
            "    pip install git+https://github.com/honeynet/droidbot.git"
        )
    path = Path(droidbot.__file__).parent
    print(f"Found droidbot at: {path}")
    return path


def main() -> int:
    root = find_droidbot()
    applied, skipped, failed = 0, 0, 0

    for patch in PATCHES:
        target = root / patch["file"]
        if not target.exists():
            print(f"  [FAIL] {patch['name']}: {target} does not exist")
            failed += 1
            continue

        content = target.read_text(encoding="utf-8")

        if patch["already"] in content:
            print(f"  [SKIP] {patch['name']}: already patched")
            skipped += 1
            continue

        if patch["old"] not in content:
            print(
                f"  [FAIL] {patch['name']}: expected code not found in "
                f"{patch['file']}; upstream may have changed"
            )
            failed += 1
            continue

        backup = target.with_suffix(target.suffix + ".orig")
        if not backup.exists():
            backup.write_text(content, encoding="utf-8")

        target.write_text(content.replace(patch["old"], patch["new"]), encoding="utf-8")
        print(f"  [OK]   {patch['name']}: patched {patch['file']}")
        applied += 1

    print(f"\nApplied {applied}, skipped {skipped}, failed {failed}.")

    # Verify DroidBot can now construct an App object end to end.
    try:
        from droidbot.app import App  # noqa: F401
        print("Verification: droidbot.app imports cleanly.")
    except Exception as exc:
        print(f"Verification FAILED: {exc}")
        return 1

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
