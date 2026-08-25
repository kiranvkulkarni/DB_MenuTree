"""One run per device at a time.

Two runs driving the same handset interleave their taps, and the damage is
invisible in the logs: each one reports a plausible walk while the other is
navigating underneath it. It also explains a device that appears to keep
acting after a run "finished" -- an earlier run with a two-hour time budget
is still going, and the finished one is not the one you are watching.

The lock is a file in the output root naming the process that holds it. A
stale lock (the process is gone) is taken over silently; a live one refuses
the new run and prints the PID to kill.
"""
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DeviceBusy(RuntimeError):
    """Another live run already holds this device."""


def _alive(pid: int) -> bool:
    """Is this PID still running? Conservative: unknown counts as alive."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, timeout=15, check=False,
            ).stdout.decode("utf-8", errors="replace")
        except Exception:
            return True
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """Context manager holding the device for the duration of a run."""

    def __init__(self, output_root: Path, serial: Optional[str]):
        # Keyed by device, not by output folder: the contended resource is the
        # handset. A default serial still collides, which is what we want when
        # only one device is attached.
        name = (serial or "default").replace(":", "_").replace("/", "_")
        self.path = Path(output_root) / f".run-lock-{name}"
        self.serial = serial
        self._held = False

    def acquire(self, force: bool = False) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not force:
            try:
                held = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                held = {}
            pid = int(held.get("pid", 0) or 0)
            if _alive(pid):
                age = int(time.time() - float(held.get("started", time.time())))
                raise DeviceBusy(
                    f"Device {self.serial or '<default>'} is already being "
                    f"driven by PID {pid}, started {age // 60}m{age % 60:02d}s "
                    f"ago:\n    {held.get('command', '?')}\n"
                    f"Stop it first (taskkill /PID {pid} /F on Windows, "
                    f"kill {pid} elsewhere), or pass --force-lock to override."
                )
            logger.info("Taking over a stale run lock from PID %s.", pid or "?")

        self.path.write_text(json.dumps({
            "pid": os.getpid(),
            "serial": self.serial,
            "started": time.time(),
            "started_human": time.strftime("%Y-%m-%d %H:%M:%S"),
            "command": " ".join(sys.argv),
        }, indent=2), encoding="utf-8")
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            self.path.unlink()
        except OSError as exc:
            logger.debug("could not remove run lock: %s", exc)

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()
