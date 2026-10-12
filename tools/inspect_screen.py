#!/usr/bin/env python3
"""Look at ONE screen and say what every element is and how it should be handled.

The walker has been developed by letting it run for an hour and reading the
wreckage afterwards. Nearly every defect found that way -- a toggle that
raised a dialog, a section header the lookup could not find, a summary line
filed as though it were a menu -- was visible on a single screen in a minute.
This tool is that minute.

    # what is on Settings, and what the walk would do with each element
    python tools/inspect_screen.py --serial <s> \\
        --path "PORTRAIT,Quick controls,Go to Settings"

    # also press each candidate, record what ACTUALLY happens, and restore
    python tools/inspect_screen.py --serial <s> --path "..." --probe

Roles come from the dump, never from guessing at wording:

    title     the first label in a clickable row          -> press
    summary   a further label in the SAME row             -> list; never press
              (pressing it opens the row's screen and files it under the
               summary -- "Go to Settings > HEVC (high efficiency)")
    toggle    Switch / checkable                          -> list; do not switch
    radio     RadioButton                                 -> list as an option
    checkbox  CheckBox                                    -> list; do not tick
    slider    SeekBar                                     -> list
    input     EditText                                    -> list
    tab       member of the tab strip                     -> handled as a node
    action    shutter, camera switch, gallery             -> list; do not perform
    back      Navigate up / Close / Back                  -> how to return
    header    text with no clickable row around it        -> list
    chrome    status / navigation bar                     -> ignore
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.elements import _pressable_via  # noqa: E402
from src.crawler.hierarchy import center_of, scrollable_container  # noqa: E402
from src.crawler.recursive_walk import ACTION_LABELS, RecursiveWalker  # noqa: E402
from src.run_lock import RunLock  # noqa: E402

SYSTEM_BAR = ("recents", "back", "home")
HANDLING = {
    "title": "press", "summary": "list only", "toggle": "list, do not switch",
    "radio": "list as option", "checkbox": "list, do not tick", "slider": "list",
    "input": "list", "tab": "node", "action": "list, do not perform",
    "back": "return route", "header": "list", "chrome": "ignore",
}


def classify(walker, elements, views):
    """(element, role) for every element, in screen order."""
    tabs = {t.label.strip().lower() for t in walker._tab_strip(elements, views)}
    seen_rows = set()
    out = []
    for e in elements:
        view = views[e.view_index] if 0 <= e.view_index < len(views) else {}
        cls = (view.get("class") or "").split(".")[-1].lower()
        label = e.label.strip().lower()
        if label in SYSTEM_BAR and e.kind in ("back", "button") and \
                (view.get("package") or "").startswith("com.android.systemui"):
            role = "chrome"
        elif walker._is_back(e):
            role = "back"
        elif label in tabs or label in walker._tab_labels:
            role = "tab"
        elif label in ACTION_LABELS:
            role = "action"
        elif "switch" in cls or e.kind == "toggle":
            role = "toggle"
        elif "radio" in cls or e.kind == "radio":
            role = "radio"
        elif "checkbox" in cls:
            role = "checkbox"
        elif "seekbar" in cls:
            role = "slider"
        elif "edittext" in cls or e.kind == "input":
            role = "input"
        else:
            row = (e.view_index if view.get("clickable")
                   else _pressable_via(views, e.view_index))
            if row is None:
                role = "header"
            elif row in seen_rows:
                role = "summary"
            else:
                seen_rows.add(row)
                role = "title"
        out.append((e, role))
    return out


def outcome(walker, before, after_views, after_pkg):
    """What a press actually did, in words."""
    if after_pkg and after_pkg != walker.package:
        return f"LEFT APP -> {after_pkg}"
    if not after_views:
        return "no readable screen"
    after = walker._elements(after_views)
    from src.crawler.elements import screen_similarity
    from src.crawler.hierarchy import looks_like_dialog
    sim = screen_similarity(before, after)
    new = [e.label for e in after
           if e.label not in {b.label for b in before}]
    if looks_like_dialog(after_views, walker.package):
        return f"DIALOG: {new[:3]}"
    if sim < walker.similarity_threshold:
        title = next((e.label for e in after if e.kind == "title"), new[:1])
        return f"OPENS SCREEN: {title}  ({len(after)} el)"
    if new:
        return f"EXPANDS IN PLACE: {new[:4]}"
    return "nothing visible changed"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--package", default="com.sec.android.app.camera")
    p.add_argument("--serial", required=True)
    p.add_argument("--path", default="", help="comma-separated labels to reach the screen")
    p.add_argument("--probe", action="store_true",
                   help="press each title/tab/unknown, record the outcome, restore")
    p.add_argument("--no-reset", action="store_true")
    args = p.parse_args()

    path = [x.strip() for x in args.path.split(",") if x.strip()]
    walker = RecursiveWalker(args.package, args.serial, {
        "output_dir": "output/inspect", "time_budget": 3600,
        "reset_before_start": not args.no_reset,
        "clear_between_paths": False,
    })

    with RunLock(Path("output"), args.serial):
        root_key, views = walker._start()
        if not root_key:
            print("could not read the launch screen")
            return 2
        root = walker._elements(views)
        walker._tab_labels = {t.label.strip().lower()
                              for t in walker._tab_strip(root, views)}
        try:
            if path and not walker._replay(path):
                print(f"could not reach: {' > '.join(path)}")
                return 2
            _, views, _ = walker._await_stable()
            elements = walker._enumerate_scrolled(views)
            _, views, _ = walker._await_stable()
            # classify against the views each element was last seen in is
            # not possible after a sweep, so classify what is visible, then
            # mark the rest as below the fold
            visible = walker._elements(views)
            roles = dict((walker._fingerprint(e), r)
                         for e, r in classify(walker, visible, views))
            container = scrollable_container(views)

            print(f"\nSCREEN  {' > '.join(path) or '<launch>'}")
            print(f"        {len(elements)} elements in a full sweep, "
                  f"{len(visible)} visible at once; scrolls in: "
                  f"{(container or {}).get('resource_id', '-')}")
            print(f"\n  {'role':<9} {'handling':<20} label")
            for e in elements:
                role = roles.get(walker._fingerprint(e), "(scrolled)")
                hand = HANDLING.get(role, "-")
                print(f"  {role:<9} {hand:<20} {e.label[:58]}")

            if args.probe:
                print("\nPROBE  (press, observe, restore)")
                for e, role in classify(walker, visible, views):
                    if role not in ("title", "tab", "toggle", "radio", "checkbox"):
                        continue
                    if role in ("toggle", "checkbox"):
                        # never actually flip these: describe only
                        print(f"  {role:<9} {e.label[:40]:<42} not pressed "
                              f"(would change a setting)")
                        continue
                    _, now, _ = walker._await_stable()
                    target = walker._find_element(e.label, now or [])
                    if target is None:
                        print(f"  {role:<9} {e.label[:40]:<42} could not locate")
                        continue
                    before = walker._elements(now)
                    centre = center_of(now[target.view_index])
                    walker.driver.tap(*centre, settle=False)
                    time.sleep(1.5)
                    _, after_views, after_pkg = walker._await_stable()
                    print(f"  {role:<9} {e.label[:40]:<42} "
                          f"{outcome(walker, before, after_views, after_pkg)}")
                    # restore
                    if path:
                        walker._replay(path)
                    else:
                        walker.driver.launch_clean(walker.package, clear=False)
                    time.sleep(1.0)
        finally:
            walker._release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
