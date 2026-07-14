import subprocess
import sys
from typing import Optional

class DroidBotRunner:
    def __init__(self, serial: str, package: str, config: dict):
        self.serial = serial
        self.package = package
        self.config = config

    def start_exploration(self) -> bool:
        print(f"[*] Starting DroidBot exploration for {self.package} on {self.serial}...")
        
        # Bypassing the broken PyPI wrapper by calling the module directly
        cmd = [
            sys.executable, "-m", "droidbot.start",
            "-d", self.serial,
            "-a", self.package,
            "-o", self.config['output_dir'],
            "-policy", self.config['policy'],
            "-timeout", str(self.config['timeout']),
            "-keep_app",
            "-keep_env"
        ]

        try:
            subprocess.run(cmd, check=True)
            return True
        except subprocess.CalledProcessError as e:
            print(f"[!] DroidBot execution interrupted or failed: {e}")
            return False