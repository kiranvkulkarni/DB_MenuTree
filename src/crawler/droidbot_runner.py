"""Drive DroidBot's exploration crawl.

Fixes over the original:
  - `-a` receives a real APK path (DroidBot's App class requires one).
  - `-count` and `-timeout` are both set explicitly, so a run is reproducible
    instead of inheriting DroidBot's effectively-unbounded defaults.
  - Optional `pm clear` + `-grant_perm` for a deterministic starting state.
  - Streamed, logged output instead of an opaque blocking subprocess.
"""
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from .apk_resolver import ApkResolutionError, resolve_apk, verify_device

logger = logging.getLogger(__name__)

VALID_POLICIES = {
    "dfs_naive", "dfs_greedy", "bfs_naive", "bfs_greedy",
    "monkey", "replay", "manual", "none",
}


class CrawlError(Exception):
    pass


class DroidBotRunner:
    def __init__(self, serial: Optional[str], package: str, config: dict):
        self.serial = serial
        self.package = package
        self.config = config
        self.output_dir = Path(config["output_dir"])
        self.policy = config.get("policy", "dfs_greedy")
        self.timeout = int(config.get("timeout", -1))
        self.count = int(config.get("count", 100000))
        self.interval = config.get("interval", 1)
        self.grant_perm = config.get("grant_perm", True)
        self.clear_app_data = config.get("clear_app_data", True)
        self.is_emulator = config.get("is_emulator", False)
        self.apk_path = config.get("apk_path")
        self.cache_dir = config.get("apk_cache_dir", "./temp_apks")

        if self.policy not in VALID_POLICIES:
            raise CrawlError(
                f"Unknown policy '{self.policy}'. Valid: {', '.join(sorted(VALID_POLICIES))}"
            )

    # -- preflight -------------------------------------------------------
    def preflight(self) -> Path:
        """Validate everything a long crawl depends on, before starting it."""
        if shutil.which("adb") is None:
            raise CrawlError("adb is not on PATH. Add Android SDK platform-tools.")

        try:
            import droidbot  # noqa: F401
        except ImportError as exc:
            raise CrawlError(
                "droidbot is not installed. Install it with:\n"
                "    pip install git+https://github.com/honeynet/droidbot.git"
            ) from exc

        try:
            self.serial = verify_device(self.serial)
            logger.info("Target device verified: %s", self.serial)

            if self.apk_path:
                apk = Path(self.apk_path)
                if not apk.exists():
                    raise CrawlError(f"Configured apk_path does not exist: {apk}")
            else:
                apk = resolve_apk(self.package, self.serial, self.cache_dir)
        except ApkResolutionError as exc:
            raise CrawlError(str(exc)) from exc

        logger.info("Using APK: %s", apk.resolve())
        return apk

    def _reset_app_state(self) -> None:
        """Deterministic starting point -- two runs must explore the same app."""
        if not self.clear_app_data:
            logger.warning(
                "clear_app_data is disabled; the crawl starts from whatever "
                "state the app was left in, so runs are not comparable."
            )
            return
        cmd = ["adb"]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += ["shell", "pm", "clear", self.package]
        logger.info("Clearing app data for %s", self.package)
        result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        if result.returncode != 0:
            logger.warning(
                "pm clear failed (%s). System apps often disallow this; the run "
                "continues but is less reproducible.",
                result.stderr.decode("utf-8", errors="replace").strip(),
            )

    # -- run -------------------------------------------------------------
    def _build_command(self, apk: Path) -> List[str]:
        cmd = [
            sys.executable, "-m", "droidbot.start",
            "-a", str(apk.resolve()),
            "-o", str(self.output_dir.resolve()),
            "-policy", self.policy,
            "-count", str(self.count),
            "-interval", str(self.interval),
            "-timeout", str(self.timeout),
            "-keep_app",
            "-keep_env",
        ]
        if self.serial:
            cmd[3:3] = ["-d", self.serial]
        if self.grant_perm:
            cmd.append("-grant_perm")
        if self.is_emulator:
            cmd.append("-is_emulator")
        return cmd

    def start_exploration(self) -> bool:
        apk = self.preflight()
        self._reset_app_state()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        cmd = self._build_command(apk)
        logger.info("Launching DroidBot: %s", " ".join(cmd))
        logger.info(
            "Budget: policy=%s count=%s timeout=%ss",
            self.policy, self.count, self.timeout,
        )

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                logger.info("[droidbot] %s", line.rstrip())
            returncode = process.wait()
        except KeyboardInterrupt:
            logger.warning("Crawl interrupted by user; partial output retained.")
            process.terminate()
            return self._utg_exists()
        except OSError as exc:
            raise CrawlError(f"Could not start DroidBot: {exc}") from exc

        if returncode != 0:
            logger.error("DroidBot exited with code %s", returncode)
            # A non-zero exit with a written UTG is still usable partial data.
            return self._utg_exists()

        if not self._utg_exists():
            raise CrawlError(
                f"DroidBot exited cleanly but wrote no utg.js to {self.output_dir}. "
                "The app likely never came to the foreground."
            )
        logger.info("Crawl complete: %s", self.output_dir.resolve())
        return True

    def _utg_exists(self) -> bool:
        utg = self.output_dir / "utg.js"
        if utg.exists():
            logger.info("Partial/complete UTG available at %s", utg)
            return True
        logger.error("No utg.js produced at %s", utg)
        return False
