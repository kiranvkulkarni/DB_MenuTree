"""Build a synthetic droidbot_out/ that matches DroidBot's real on-disk format.

Lets the parser -> emitter -> coverage chain be exercised without a device.
Mirrors the exact shapes DroidBot writes:
  utg.js                 -> "var utg = \\n" + JSON {nodes, edges, ...}
  events/event_<tag>.json -> {tag, event, start_state, stop_state, event_str}
"""
import json
import shutil
from pathlib import Path

PACKAGE = "com.sec.android.app.camera"
MAIN_ACTIVITY = "com.sec.android.app.camera.Camera"
SETTINGS_ACTIVITY = "com.sec.android.app.camera.SettingsActivity"

# (event_id, from, to, activity_of_from, text, desc, resource_id, event_type)
EDGES = [
    (1, "S0", "S1", MAIN_ACTIVITY, "Flash", None, "flash_btn", "touch"),
    (2, "S1", "S2", MAIN_ACTIVITY, "Auto", None, None, "touch"),
    (3, "S0", "S3", MAIN_ACTIVITY, None, "Resolution", None, "touch"),
    (4, "S0", "S4", MAIN_ACTIVITY, "Filters", None, None, "touch"),
    (5, "S4", "S5", MAIN_ACTIVITY, None, "Original", None, "touch"),
    (6, "S0", "S6", MAIN_ACTIVITY, None, "Settings", "settings_btn", "touch"),
    (7, "S6", "S7", SETTINGS_ACTIVITY, "Video size", None, None, "touch"),
    (8, "S1", "S0", MAIN_ACTIVITY, None, None, None, "key"),  # BACK
]

STATE_ACTIVITY = {
    "S0": MAIN_ACTIVITY, "S1": MAIN_ACTIVITY, "S2": MAIN_ACTIVITY,
    "S3": MAIN_ACTIVITY, "S4": MAIN_ACTIVITY, "S5": MAIN_ACTIVITY,
    "S6": SETTINGS_ACTIVITY, "S7": SETTINGS_ACTIVITY,
    "S8": "com.sec.android.app.camera.OrphanActivity",  # unreachable on purpose
}


def _event_str(edge):
    event_id, src, _dst, _act, text, desc, rid, etype = edge
    if etype == "key":
        return "KeyEvent(name=BACK)"
    ident = text or desc or rid or "?"
    return f"TouchEvent(state={src}, view={ident}_{event_id})"


def build(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root)
    (root / "events").mkdir(parents=True)
    (root / "states").mkdir(parents=True)

    nodes = []
    for state, activity in STATE_ACTIVITY.items():
        label = activity.split(".")[-1]
        if state == "S0":
            label += "\n<FIRST>"
        nodes.append({
            "id": state,
            "shape": "image",
            "image": f"states/screen_{state}.png",
            "label": label,
            "package": PACKAGE,
            "activity": activity,
            "state_str": state,
            "structure_str": f"struct_{state}",
            "title": "",
            "content": "",
        })

    edges_by_pair = {}
    for edge in EDGES:
        event_id, src, dst, _act, _t, _d, _r, etype = edge
        edges_by_pair.setdefault((src, dst), []).append({
            "event_str": _event_str(edge),
            "event_id": event_id,
            "event_type": etype,
            "view_images": [],
        })

    utg = {
        "nodes": nodes,
        "edges": [
            {"from": src, "to": dst, "id": f"{src}-->{dst}",
             "title": "", "label": "", "events": events}
            for (src, dst), events in edges_by_pair.items()
        ],
        "num_nodes": len(nodes),
        "num_edges": len(edges_by_pair),
        "num_effective_events": len(EDGES),
        "num_reached_activities": len(set(STATE_ACTIVITY.values())),
        "test_date": "2026-08-19 01:00:00",
        "time_spent": 1830.0,
        "num_transitions": len(EDGES),
        "device_serial": "FIXTURE01",
        "device_model_number": "SM-FIXTURE",
        "device_sdk_version": 34,
        "app_sha256": "0" * 64,
        "app_package": PACKAGE,
        "app_main_activity": MAIN_ACTIVITY,
        "app_num_total_activities": 4,
    }
    (root / "utg.js").write_text(
        "var utg = \n" + json.dumps(utg, indent=2), encoding="utf-8"
    )

    for edge in EDGES:
        event_id, src, dst, _act, text, desc, rid, etype = edge
        if etype == "key":
            event = {"event_type": "key", "name": "BACK"}
        else:
            event = {
                "event_type": etype,
                "view": {
                    "text": text,
                    "content_description": desc,
                    "resource_id": f"{PACKAGE}:id/{rid}" if rid else None,
                    "class": "android.widget.Button",
                },
            }
        record = {
            "tag": f"2026-08-19_0100{event_id:02d}",
            "event": event,
            "start_state": src,
            "stop_state": dst,
            "event_str": _event_str(edge),
        }
        (root / "events" / f"event_{record['tag']}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )

    return root


if __name__ == "__main__":
    out = build(Path("./tests/fixture_out"))
    print(f"Fixture written to {out.resolve()}")
