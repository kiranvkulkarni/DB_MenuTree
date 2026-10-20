"""What a MenuTree was generated on: phone, software, build, hardware, app.

Agreed 2026-10-20: every workbook says plainly which phone, which software
build and which app version its tree came from, and how big the tree is.
Read once over adb at the start of a run (read-only: getprop, dumpsys,
settings get -- nothing on the phone is changed) and kept in
`<run>_run_info.json`; the workbook's Summary sheet is written from it.

Nothing here knows any app or any vendor. Properties a phone does not have
are simply left out.
"""
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# (key, label, system property) -- read in this order, blank ones dropped.
DEVICE_PROPS: Tuple[Tuple[str, str, str], ...] = (
    ("manufacturer", "Manufacturer", "ro.product.manufacturer"),
    ("model", "Model number", "ro.product.model"),
    ("product", "Product name", "ro.product.name"),
    ("device", "Device code", "ro.product.device"),
    ("android", "Android version", "ro.build.version.release"),
    ("sdk", "API level", "ro.build.version.sdk"),
    ("oneui", "One UI version", "ro.build.version.oneui"),
    ("security_patch", "Security patch", "ro.build.version.security_patch"),
    ("build_number", "Build number", "ro.build.display.id"),
    ("build_id", "Build ID", "ro.build.id"),
    ("incremental", "Build incremental", "ro.build.version.incremental"),
    ("ap", "AP (PDA) version", "ro.build.PDA"),
    ("cp", "CP (baseband) version", "gsm.version.baseband"),
    ("csc", "CSC version", "ril.official_cscver"),
    ("sales_code", "Sales code", "ro.csc.sales_code"),
    ("build_type", "Build type", "ro.build.type"),
    ("build_tags", "Build tags", "ro.build.tags"),
    ("build_date", "Build date", "ro.build.date"),
    ("fingerprint", "Build fingerprint", "ro.build.fingerprint"),
    ("hw_revision", "Hardware revision", "ro.revision"),
    ("hw_revision_boot", "Hardware revision (boot)", "ro.boot.revision"),
    ("soc", "SoC model", "ro.soc.model"),
    ("soc_maker", "SoC manufacturer", "ro.soc.manufacturer"),
    ("hardware", "Hardware platform", "ro.hardware"),
    ("board", "Board", "ro.product.board"),
    ("bootloader", "Bootloader", "ro.bootloader"),
    ("locale", "Locale", "persist.sys.locale"),
)

# Which device keys fall under which heading of the Summary sheet.
SECTIONS = (
    ("Phone", ("device_name", "manufacturer", "model", "product", "device", "serial",
               "screen_size", "screen_density", "locale")),
    ("Software version", ("android", "sdk", "oneui", "security_patch")),
    ("Build version", ("build_number", "build_id", "incremental", "ap", "cp", "csc",
                       "sales_code", "build_type", "build_tags", "build_date",
                       "fingerprint")),
    ("Hardware version", ("hw_revision", "hw_revision_boot", "soc", "soc_maker",
                          "hardware", "board", "bootloader")),
)

LABELS = {key: label for key, label, _ in DEVICE_PROPS}
LABELS.update({
    "device_name": "Device name",
    "serial": "Serial number",
    "screen_size": "Screen size (px)",
    "screen_density": "Screen density (dpi)",
})


def _adb(serial: Optional[str], *args: str, timeout: int = 20) -> str:
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(args)
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", errors="replace")


def _props(serial: Optional[str]) -> Dict[str, str]:
    """Every system property, from one `getprop`."""
    props = {}
    for line in _adb(serial, "shell", "getprop").splitlines():
        line = line.strip()
        if line.startswith("[") and "]: [" in line and line.endswith("]"):
            key, value = line[1:-1].split("]: [", 1)
            props[key] = value.strip()
    return props


def _readable_oneui(raw: str) -> str:
    """'80500' -> '8.5' (major*10000 + minor*100 + patch)."""
    if raw.isdigit() and len(raw) >= 5:
        n = int(raw)
        major, minor, patch = n // 10000, (n // 100) % 100, n % 100
        text = f"{major}.{minor}" + (f".{patch}" if patch else "")
        return f"{text} ({raw})"
    return raw


def device_info(serial: Optional[str]) -> Dict[str, str]:
    props = _props(serial)
    if not props:
        return {}
    info: Dict[str, str] = {}
    name = _adb(serial, "shell", "settings", "get", "global", "device_name").strip()
    if name and name != "null":
        info["device_name"] = name
    for key, _, prop in DEVICE_PROPS:
        value = props.get(prop, "")
        if value:
            info[key] = value
    if "oneui" in info:
        info["oneui"] = _readable_oneui(info["oneui"])
    if "locale" not in info and props.get("ro.product.locale"):
        info["locale"] = props["ro.product.locale"]
    serial_no = serial or _adb(None, "get-serialno").strip()
    if serial_no:
        info["serial"] = serial_no
    size = _adb(serial, "shell", "wm", "size")
    for line in size.splitlines()[::-1]:      # an override line comes last
        if ":" in line:
            info["screen_size"] = line.split(":", 1)[1].strip()
            break
    density = _adb(serial, "shell", "wm", "density")
    for line in density.splitlines()[::-1]:
        if ":" in line:
            info["screen_density"] = line.split(":", 1)[1].strip()
            break
    return info


def app_info(serial: Optional[str], package: str) -> Dict[str, str]:
    """Version and install details of the app, from `dumpsys package`."""
    out = _adb(serial, "shell", "dumpsys", "package", package)
    info: Dict[str, str] = {}
    for raw in out.splitlines():
        line = raw.strip()
        for token in line.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            if key in ("versionName", "versionCode", "minSdk", "targetSdk",
                       "codePath") and key not in info:
                info[key] = value
        for key in ("firstInstallTime", "lastUpdateTime"):
            # An app built into the system image reports the epoch; no date.
            if line.startswith(key + "=") and key not in info                     and not line.split("=", 1)[1].startswith("1970"):
                info[key] = line.split("=", 1)[1]
    return info


def tool_version(root: Optional[Path] = None) -> str:
    """The commit the tool ran from, with '+changes' when the tree is dirty."""
    root = root or Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                                "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return head + (" +changes" if head and dirty else "")


def collect(package: str, serial: Optional[str], app_name: str = "") -> Dict:
    """Read the phone and the app now. Empty sections when no phone answers."""
    return {
        "format": "run-info/1",
        "recorded": datetime.now().isoformat(timespec="seconds"),
        "package": package,
        "app_name": app_name,
        "app": app_info(serial, package),
        "device": device_info(serial),
        "tool": tool_version(),
    }


def save(info: Dict, path: Path) -> Path:
    path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load(path: Optional[Path]) -> Dict:
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _duration(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    return f"{s // 3600} h {s % 3600 // 60:02d} min" if s >= 3600 else f"{s // 60} min {s % 60:02d} s"


def tree_figures(rows: Sequence[Dict]) -> List[Tuple[str, object]]:
    """Size and shape of the tree: depths, rows per depth, rows per kind."""
    if not rows:
        return []
    depths = [int(r.get("depth", 1)) for r in rows]
    deepest = max(depths)
    with_widget = max(d + (1 if r.get("widget") else 0) for d, r in zip(depths, rows))
    # Depth 1 is the app's own root row when the walk wrote one; the
    # top-level items are the rows under it.
    root = min((int(r.get("depth", 1)) for r in rows if r.get("kind") == "root"), default=0)
    top = [r for r in rows if int(r.get("depth", 1)) == root + 1]
    out: List[Tuple[str, object]] = [
        ("Total rows", len(rows)),
        ("Deepest menu level (depth)", deepest),
        ("Depth columns used (with switch/value column)", with_widget),
        (f"Top-level items (depth {root + 1})", len(top)),
        ("Rows with a switch / value beside them", sum(1 for r in rows if r.get("widget"))),
        ("Long-press rows", sum(1 for r in rows
                                if (r.get("label") or "").endswith(" (long press)"))),
        ("Rows withheld by the action guard", sum(1 for r in rows if r.get("blocked"))),
    ]
    for d in range(1, deepest + 1):
        out.append((f"Rows at depth {d}", depths.count(d)))
    kinds: Dict[str, int] = {}
    for r in rows:
        kinds[r.get("kind") or "(none)"] = kinds.get(r.get("kind") or "(none)", 0) + 1
    for kind, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        out.append((f"Rows of kind '{kind}'", n))
    return out


def run_figures(stats: Dict, run: Dict) -> List[Tuple[str, object]]:
    """How the walk went: times, presses, returns, completeness."""
    out: List[Tuple[str, object]] = []
    for key, label in (("started", "Walk started"), ("finished", "Walk finished")):
        if run.get(key):
            out.append((label, run[key]))
    if stats.get("elapsed_seconds") is not None:
        out.append(("Walk duration", _duration(stats["elapsed_seconds"])))
    if run.get("time_budget"):
        out.append(("Time budget", _duration(run["time_budget"])))
    if "complete" in run:
        out.append(("Walk complete", "Yes" if run["complete"] else
                    f"No -- ended early: {run.get('ended_early', '')}"))
    # The recursive walker's names first; the older worklist walker's after.
    for keys, label in ((("nodes_visited", "screens_visited"), "Screens and panels walked"),
                        (("panels_listed",), "Panels listed"),
                        (("clicks",), "Presses"),
                        (("returns_ok", "back_ok"), "Returns OK"),
                        (("returns_failed", "back_failed"), "Returns failed"),
                        (("relaunches",), "App relaunches"),
                        (("separate_passes_walked",), "Separate passes walked"),
                        (("first_launch_answers_pressed",), "First-launch answers pressed"),
                        (("switches_restored",), "Switches put back"),
                        (("switches_not_restored",), "Switches NOT put back")):
        value = next((stats[k] for k in keys if stats.get(k)), None)
        if value is None:
            value = next((stats[k] for k in keys if k in stats), None)
        if value is not None:
            out.append((label, value))
    guard = stats.get("guard") or {}
    if guard.get("blocked_attempts") is not None:
        out.append(("Presses refused by the action guard", guard["blocked_attempts"]))
    for key, label in (("reset", "App data cleared before the walk"),
                       ("command", "Command")):
        if key in run:
            value = run[key]
            out.append((label, ("Yes" if value else "No") if isinstance(value, bool) else value))
    return out


def add_sheet(book, info: Dict, rows: Sequence[Dict], stats: Optional[Dict],
              extra: Sequence[Tuple[str, object]] = ()) -> None:
    """The Summary sheet: first in the workbook, two columns, by section."""
    from openpyxl.styles import Alignment, Font, PatternFill
    sheet = book.create_sheet("Summary", 0)
    head_fill = PatternFill("solid", fgColor="1F4E79")
    key_fill = PatternFill("solid", fgColor="DDEBF7")
    top = Alignment(vertical="top", wrap_text=True)
    line = [1]

    def title(text):
        if line[0] > 1:
            line[0] += 1
        for col in (1, 2):
            cell = sheet.cell(row=line[0], column=col, value=text if col == 1 else None)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
        line[0] += 1

    def item(label, value):
        key = sheet.cell(row=line[0], column=1, value=label)
        key.font = Font(bold=True)
        key.fill = key_fill
        key.alignment = top
        if value is None:
            value = ""
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            value = str(value)
        sheet.cell(row=line[0], column=2, value=value).alignment = top
        line[0] += 1

    run = info.get("run") or {}
    app = info.get("app") or {}
    device = info.get("device") or {}

    title("MenuTree")
    item("Generated", run.get("finished") or info.get("recorded", ""))
    item("Run folder", run.get("folder", ""))
    item("Tool version (git)", info.get("tool", ""))
    if info.get("recorded_after_walk"):
        item("Phone details read", f"{info.get('recorded')} -- after the walk, "
             "from the same phone; not recorded at the time of the walk")

    title("Application")
    item("Application name", info.get("app_name") or "(not given in the profile)")
    item("Package name", info.get("package", ""))
    for key, label in (("versionName", "Version"), ("versionCode", "Version code"),
                       ("targetSdk", "Target SDK"), ("minSdk", "Minimum SDK"),
                       ("codePath", "Installed at"), ("lastUpdateTime", "Last updated")):
        if app.get(key):
            item(label, app[key])

    if not device:
        title("Phone")
        item("Phone details", "Not recorded (no phone answered when this was written)")
    for section, keys in SECTIONS:
        present = [k for k in keys if device.get(k)]
        if not present:
            continue
        title(section)
        for key in present:
            item(LABELS.get(key, key), device[key])

    title("Tree")
    for label, value in list(tree_figures(rows)) + list(extra):
        item(label, value)

    figures = run_figures(stats or {}, run)
    if figures:
        title("Walk")
        for label, value in figures:
            item(label, value)

    sheet.column_dimensions["A"].width = 44
    sheet.column_dimensions["B"].width = 80
    sheet.freeze_panes = "A2"
