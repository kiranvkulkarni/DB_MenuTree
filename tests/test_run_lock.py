"""Offline checks for the per-device run lock.

Covers the case it exists for: a second run must refuse to start while a live
run is driving the same handset, and must take over a lock whose owner is
gone. Uses a real child process rather than a fabricated PID -- the first
version of this test planted the PID of a process that had already exited,
which the lock correctly treated as stale, so the test passed for the wrong
reason and proved nothing.

    python tests/test_run_lock.py
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.run_lock import DeviceBusy, RunLock, _alive  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def main() -> int:
    ok = True
    root = Path(tempfile.mkdtemp(prefix="runlock_"))

    print("liveness")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ok &= check("a running process reads as alive", _alive(child.pid), str(child.pid))
    ok &= check("PID 0 is never alive", not _alive(0))

    print("\nmutual exclusion")
    held = RunLock(root, "SERIAL_A")
    held.path.write_text(json.dumps({
        "pid": child.pid, "serial": "SERIAL_A",
        "started": time.time() - 300, "command": "an earlier run",
    }), encoding="utf-8")

    try:
        RunLock(root, "SERIAL_A").acquire()
        ok &= check("second run on the same device is refused", False)
    except DeviceBusy as exc:
        ok &= check("second run on the same device is refused", True)
        ok &= check("the message names the PID to kill", str(child.pid) in str(exc))

    other = RunLock(root, "SERIAL_B")
    try:
        other.acquire().release()
        ok &= check("a different device is not blocked", True)
    except DeviceBusy:
        ok &= check("a different device is not blocked", False)

    forced = RunLock(root, "SERIAL_A")
    try:
        forced.acquire(force=True)
        ok &= check("--force-lock overrides a live lock", True)
        forced.release()
    except DeviceBusy:
        ok &= check("--force-lock overrides a live lock", False)

    print("\nstale locks")
    child.kill()
    child.wait()
    ok &= check("a dead process reads as not alive", not _alive(child.pid))
    stale = RunLock(root, "SERIAL_A")
    stale.path.write_text(json.dumps({
        "pid": child.pid, "serial": "SERIAL_A",
        "started": time.time() - 300, "command": "a run that died",
    }), encoding="utf-8")
    try:
        stale.acquire()
        ok &= check("a stale lock is taken over", True)
    except DeviceBusy:
        ok &= check("a stale lock is taken over", False)
    ok &= check("release removes the lock file",
                (stale.release() or True) and not stale.path.exists())

    print("\ncorrupt locks")
    broken = RunLock(root, "SERIAL_C")
    broken.path.write_text("not json at all", encoding="utf-8")
    try:
        broken.acquire().release()
        ok &= check("an unreadable lock does not wedge the tool", True)
    except DeviceBusy:
        ok &= check("an unreadable lock does not wedge the tool", False)

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
