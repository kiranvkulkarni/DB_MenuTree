"""Resolve a package name to a local APK file.

DroidBot's `-a` flag needs a *file path*: its App class calls
androguard's APK(app_path) directly, so a package name string fails outright.
Preinstalled system apps (the Samsung camera among them) are usually split
APKs; androguard cannot open a split set as one file, so we pull the base
APK specifically and say so plainly when only splits exist.
"""
import logging
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

ADB_TIMEOUT = 120


class ApkResolutionError(Exception):
    pass


def _adb(serial: Optional[str], *args: str, binary: bool = False):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, timeout=ADB_TIMEOUT, check=False
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ApkResolutionError(f"adb {' '.join(args)} failed: {stderr}")
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")


def list_apk_paths(package: str, serial: Optional[str] = None) -> List[str]:
    out = _adb(serial, "shell", "pm", "path", package)
    paths = [
        line.strip()[len("package:"):]
        for line in out.splitlines()
        if line.strip().startswith("package:")
    ]
    if not paths:
        raise ApkResolutionError(
            f"Package '{package}' is not installed on device "
            f"'{serial or 'default'}' (pm path returned nothing)."
        )
    return paths


def resolve_apk(
    package: str, serial: Optional[str] = None, cache_dir: str = "./temp_apks"
) -> Path:
    """Pull the base APK for `package` and return its local path.

    Cached: a second run reuses the pulled file.
    """
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    local_path = cache / f"{package}.apk"
    if local_path.exists() and local_path.stat().st_size > 0:
        logger.info("Reusing cached APK: %s", local_path)
        return local_path

    remote_paths = list_apk_paths(package, serial)
    logger.info("Device reports %d APK file(s) for %s", len(remote_paths), package)

    base_candidates = [p for p in remote_paths if "/split_" not in p]
    if not base_candidates:
        raise ApkResolutionError(
            f"'{package}' is installed only as split APKs "
            f"({len(remote_paths)} parts) with no identifiable base. androguard "
            "cannot read a split set as a single file. Options: supply a "
            "standalone APK via crawler.apk_path, or patch DroidBot's App class "
            "to accept a package name and read activities from `dumpsys package`."
        )
    if len(base_candidates) > 1:
        logger.warning(
            "Multiple non-split APKs found; using the first: %s", base_candidates[0]
        )

    remote = base_candidates[0]
    logger.info("Pulling %s -> %s", remote, local_path)
    _adb(serial, "pull", remote, str(local_path))

    if not local_path.exists() or local_path.stat().st_size == 0:
        raise ApkResolutionError(f"adb pull produced no usable file at {local_path}")

    if len(remote_paths) > 1:
        logger.warning(
            "%s has %d split APK(s) alongside the base. DroidBot will read "
            "activities from the base manifest only, so activity coverage may "
            "under-count screens defined in splits.",
            package,
            len(remote_paths) - 1,
        )
    return local_path


def verify_device(serial: Optional[str] = None) -> str:
    """Confirm the target device is present and authorized before a long crawl."""
    out = _adb(None, "devices")
    devices = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devices[parts[0]] = parts[1]

    if not devices:
        raise ApkResolutionError("No devices found by `adb devices`.")

    if serial:
        state = devices.get(serial)
        if state is None:
            raise ApkResolutionError(
                f"Device '{serial}' not found. Visible: {', '.join(devices) or 'none'}"
            )
        if state != "device":
            raise ApkResolutionError(
                f"Device '{serial}' is in state '{state}', not 'device'. "
                "Re-authorize USB debugging and accept the RSA prompt."
            )
        return serial

    online = [s for s, st in devices.items() if st == "device"]
    if len(online) != 1:
        raise ApkResolutionError(
            f"Expected exactly one online device when no serial is set; found "
            f"{len(online)}: {', '.join(online) or 'none'}. Set DEFAULT_SERIAL."
        )
    return online[0]
