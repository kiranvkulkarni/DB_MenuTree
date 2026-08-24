"""Minimal device driver: dump the screen, tap it, manage the app.

Two back-ends behind one interface:

  U2Driver  -- uiautomator2. Fast (persistent agent, HTTP), richer control.
  AdbDriver -- plain `adb shell uiautomator dump` + `input tap`. Slower, but
               needs no agent on the device, so it survives Android versions
               the uiautomator2 agent has not caught up with.

The explorer targets this interface only, so swapping back-ends never touches
exploration logic.
"""
import logging
import subprocess
import time
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

ADB_TIMEOUT = 60


class DriverError(Exception):
    pass


class DeviceDriver(Protocol):
    def dump_hierarchy(self) -> str: ...
    def tap(self, x: int, y: int) -> None: ...
    def long_tap(self, x: int, y: int) -> None: ...
    def press_back(self) -> None: ...
    def start_app(self, package: str, clear: bool = False) -> None: ...
    def stop_app(self, package: str) -> None: ...
    def current_package(self) -> Optional[str]: ...
    def current_activity(self) -> Optional[str]: ...
    def current_ime_package(self) -> Optional[str]: ...
    def screen_size(self) -> tuple: ...
    def screenshot(self, path: str) -> bool: ...


def _adb(serial: Optional[str], *args: str, timeout: int = ADB_TIMEOUT) -> str:
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise DriverError(f"adb {' '.join(args)}: {stderr}")
    return result.stdout.decode("utf-8", errors="replace")


class AdbDriver:
    """No-agent fallback. Every dump is a file round-trip, so it is slow."""

    name = "adb"

    def __init__(self, serial: Optional[str], settle_seconds: float = 1.0):
        self.serial = serial
        self.settle = settle_seconds

    def dump_hierarchy(self) -> str:
        remote = "/sdcard/menutree_dump.xml"
        _adb(self.serial, "shell", "uiautomator", "dump", remote)
        return _adb(self.serial, "shell", "cat", remote)

    def tap(self, x: int, y: int) -> None:
        _adb(self.serial, "shell", "input", "tap", str(x), str(y))
        time.sleep(self.settle)

    def long_tap(self, x: int, y: int) -> None:
        _adb(self.serial, "shell", "input", "swipe", str(x), str(y), str(x), str(y), "800")
        time.sleep(self.settle)

    def press_back(self) -> None:
        _adb(self.serial, "shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(self.settle)

    def start_app(self, package: str, clear: bool = False) -> None:
        if clear:
            try:
                _adb(self.serial, "shell", "pm", "clear", package)
            except DriverError as exc:
                logger.debug("pm clear failed for %s: %s", package, exc)
        _adb(self.serial, "shell", "monkey", "-p", package, "-c",
             "android.intent.category.LAUNCHER", "1")
        time.sleep(self.settle * 2)

    def stop_app(self, package: str) -> None:
        _adb(self.serial, "shell", "am", "force-stop", package)
        time.sleep(self.settle)

    def current_package(self) -> Optional[str]:
        try:
            out = _adb(self.serial, "shell", "dumpsys", "activity", "activities")
        except DriverError:
            return None
        for line in out.splitlines():
            if "mResumedActivity" in line or "topResumedActivity" in line:
                for token in line.split():
                    if "/" in token and "." in token:
                        return token.split("/")[0].split("{")[-1]
        return None

    def current_activity(self) -> Optional[str]:
        try:
            out = _adb(self.serial, "shell", "dumpsys", "activity", "activities")
        except DriverError:
            return None
        for line in out.splitlines():
            if "mResumedActivity" in line or "topResumedActivity" in line:
                for token in line.split():
                    if "/" in token and "." in token:
                        pkg, _, act = token.split("{")[-1].partition("/")
                        return f"{pkg}{act}" if act.startswith(".") else act
        return None



    def screen_size(self) -> tuple:
        """(width, height) in pixels, from `wm size`."""
        out = _adb(self.serial, "shell", "wm", "size")
        for line in out.splitlines():
            if "size:" in line and "x" in line:
                dims = line.split(":")[-1].strip()
                w, _, h = dims.partition("x")
                return int(w), int(h)
        raise DriverError(f"could not parse screen size from {out!r}")

    def current_ime_package(self) -> Optional[str]:
        """Active keyboard package, so its keys can be excluded from the tree."""
        try:
            out = _adb(self.serial, "shell", "settings", "get", "secure",
                       "default_input_method")
        except DriverError:
            return None
        value = out.strip()
        return value.split("/")[0] if "/" in value else (value or None)

    def screenshot(self, path: str) -> bool:
        try:
            remote = "/sdcard/menutree_shot.png"
            _adb(self.serial, "shell", "screencap", "-p", remote)
            _adb(self.serial, "pull", remote, path)
            return True
        except DriverError:
            return False


class U2Driver:
    """uiautomator2 back-end."""

    name = "uiautomator2"

    def __init__(self, serial: Optional[str], settle_seconds: float = 1.0):
        import uiautomator2 as u2

        self.settle = settle_seconds
        self.device = u2.connect(serial) if serial else u2.connect()
        self.serial = serial

    def dump_hierarchy(self) -> str:
        return self.device.dump_hierarchy()

    def tap(self, x: int, y: int) -> None:
        self.device.click(x, y)
        time.sleep(self.settle)

    def long_tap(self, x: int, y: int) -> None:
        self.device.long_click(x, y, duration=0.8)
        time.sleep(self.settle)

    def press_back(self) -> None:
        self.device.press("back")
        time.sleep(self.settle)

    def start_app(self, package: str, clear: bool = False) -> None:
        if clear:
            try:
                self.device.app_clear(package)
            except Exception as exc:
                logger.debug("app_clear failed for %s: %s", package, exc)
        self.device.app_start(package, stop=True)
        time.sleep(self.settle * 2)

    def stop_app(self, package: str) -> None:
        self.device.app_stop(package)
        time.sleep(self.settle)

    def current_package(self) -> Optional[str]:
        try:
            return self.device.app_current().get("package")
        except Exception:
            return None

    def current_activity(self) -> Optional[str]:
        try:
            info = self.device.app_current()
        except Exception:
            return None
        pkg, act = info.get("package") or "", info.get("activity") or ""
        if act.startswith("."):
            return f"{pkg}{act}"
        return act or None



    def screen_size(self) -> tuple:
        """(width, height) in pixels, from `wm size`."""
        out = _adb(self.serial, "shell", "wm", "size")
        for line in out.splitlines():
            if "size:" in line and "x" in line:
                dims = line.split(":")[-1].strip()
                w, _, h = dims.partition("x")
                return int(w), int(h)
        raise DriverError(f"could not parse screen size from {out!r}")

    def current_ime_package(self) -> Optional[str]:
        """Active keyboard package, so its keys can be excluded from the tree."""
        try:
            out = _adb(self.serial, "shell", "settings", "get", "secure",
                       "default_input_method")
        except DriverError:
            return None
        value = out.strip()
        return value.split("/")[0] if "/" in value else (value or None)


    def screen_size(self) -> tuple:
        """(width, height) in pixels."""
        info = self.device.info
        return int(info["displayWidth"]), int(info["displayHeight"])

    def screenshot(self, path: str) -> bool:
        try:
            self.device.screenshot(path)
            return True
        except Exception:
            return False


def make_driver(
    serial: Optional[str], backend: str = "auto", settle_seconds: float = 1.0
) -> DeviceDriver:
    """Build a driver, falling back to raw adb when the u2 agent will not start."""
    if backend in ("u2", "uiautomator2", "auto"):
        try:
            driver = U2Driver(serial, settle_seconds)
            driver.dump_hierarchy()
            logger.info("Device driver: uiautomator2")
            return driver
        except Exception as exc:
            if backend != "auto":
                raise DriverError(f"uiautomator2 back-end unavailable: {exc}") from exc
            logger.warning(
                "uiautomator2 unavailable (%s); falling back to the adb driver. "
                "Crawls will be slower but need no on-device agent.",
                str(exc).splitlines()[0][:160],
            )
    logger.info("Device driver: adb")
    return AdbDriver(serial, settle_seconds)
