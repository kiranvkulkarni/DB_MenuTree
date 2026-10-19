import logging
import sys
from pathlib import Path

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: str = "./logs", level: str = "INFO", run_id: str = "run") -> Path:
    """Configure root logging to stream + a per-run file.

    Returns the path of the log file. Release gating needs timestamped,
    greppable, persisted logs -- not print().
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / f"{run_id}.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # A Windows console cannot print every character a screen carries (the
    # Internet app puts U+200E before each URL); substitute, never fail.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    return log_file
