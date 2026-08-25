"""Offline checks for hierarchy parsing, state keys and selector resolution.

Uses a Compose-shaped dump: a bare clickable `android.view.View` wrapper whose
label lives on a child, which is the case that breaks naive selector logic.

    python tests/test_hierarchy.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.hierarchy import (  # noqa: E402
    center_of,
    foreground_package,
    interactive_views,
    parse_hierarchy,
    state_key,
)
from src.parser.selectors import SelectorResolver  # noqa: E402

PKG = "com.jewelestimate.app"


def dump(rate_text: str, purity: str = "22K") -> str:
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
 <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
   package="{PKG}" content-desc="" checkable="false" checked="false"
   clickable="false" enabled="true" focusable="false" scrollable="false"
   long-clickable="false" password="false" selected="false"
   bounds="[0,0][1080,2400]">
  <node index="0" text="{rate_text}" resource-id="" class="android.widget.TextView"
    package="{PKG}" content-desc="" checkable="false" checked="false"
    clickable="false" enabled="true" focusable="false" scrollable="false"
    long-clickable="false" password="false" selected="false"
    bounds="[40,300][600,380]" />
  <node index="1" text="" resource-id="" class="android.view.View"
    package="{PKG}" content-desc="" checkable="false" checked="false"
    clickable="true" enabled="true" focusable="true" scrollable="false"
    long-clickable="false" password="false" selected="false"
    bounds="[984,387][1128,531]">
   <node index="0" text="" resource-id="" class="android.view.View"
     package="{PKG}" content-desc="Refresh GOLD rate" checkable="false"
     checked="false" clickable="false" enabled="true" focusable="false"
     scrollable="false" long-clickable="false" password="false"
     selected="false" bounds="[984,387][1128,531]" />
  </node>
  <node index="2" text="{purity}" resource-id="{PKG}:id/purity_field"
    class="android.widget.Button" package="{PKG}" content-desc=""
    checkable="false" checked="false" clickable="true" enabled="true"
    focusable="true" scrollable="false" long-clickable="false"
    password="false" selected="false" bounds="[48,830][608,950]" />
  <node index="3" text="12:04" resource-id="" class="android.widget.TextView"
    package="com.android.systemui" content-desc="" checkable="false"
    checked="false" clickable="false" enabled="true" focusable="false"
    scrollable="false" long-clickable="false" password="false"
    selected="false" bounds="[0,0][120,60]" />
 </node>
</hierarchy>"""


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f"  -- {detail}" if detail else ""))
    return condition


def main() -> int:
    ok = True
    views = parse_hierarchy(dump("Rs 15589 / g"))

    print("parse_hierarchy")
    ok &= check("all nodes parsed", len(views) == 6, f"{len(views)} views")
    wrapper = next(v for v in views if v["clickable"] and v["class"].endswith("View"))
    ok &= check("clickable wrapper has no own label",
                not (wrapper["text"] or wrapper["content_description"]))
    ok &= check("wrapper has a child", len(wrapper["children"]) == 1)

    print("\nselector resolution (the Compose case)")
    resolver = SelectorResolver()
    sel = resolver.resolve(wrapper, views)
    ok &= check("label recovered from descendant",
                sel is not None and sel.strategy == "desc"
                and sel.value == "Refresh GOLD rate",
                str(sel))
    ok &= check("counted as a descendant hit", resolver.descendant_hits == 1)

    naive = SelectorResolver(resolve_descendants=False).resolve(wrapper, views)
    ok &= check("without it, falls back to xpath -- unique but fragile",
                naive is not None and naive.strategy == "xpath"
                and naive.is_fragile and not naive.is_ambiguous,
                str(naive))

    print("\ninteractive views")
    idx = interactive_views(views, PKG)
    ok &= check("systemui clock excluded", len(idx) == 2, f"indices {idx}")

    print("\nselector priority: text > desc > id > xpath")
    priority_resolver = SelectorResolver()
    by_strategy = {}
    for v in views:
        s = priority_resolver.resolve(v, views)
        if s:
            by_strategy.setdefault(s.strategy, []).append(s.value)
    ok &= check("desc used when there is no text",
                "Refresh GOLD rate" in by_strategy.get("desc", []))
    ok &= check("xpath used only as a last resort",
                all(v.startswith("/") for v in by_strategy.get("xpath", [])),
                f"{len(by_strategy.get('xpath', []))} xpath selector(s)")
    ok &= check("no ambiguous class selectors emitted",
                "class" not in by_strategy, str(sorted(by_strategy)))

    print("\nstate key stability")
    a = state_key(parse_hierarchy(dump("Rs 15589 / g")), "affordance", PKG)
    b = state_key(parse_hierarchy(dump("Rs 15612 / g")), "affordance", PKG)
    ok &= check("volatile display text does NOT change the key", a == b,
                f"{a[:10]} vs {b[:10]}")

    c = state_key(parse_hierarchy(dump("Rs 15589 / g", purity="18K")),
                  "affordance", PKG)
    ok &= check("affordance text DOES change the key", a != c,
                f"{a[:10]} vs {c[:10]}")

    d = state_key(parse_hierarchy(dump("Rs 15589 / g")), "content", PKG)
    e = state_key(parse_hierarchy(dump("Rs 15612 / g")), "content", PKG)
    ok &= check("content mode is sensitive to display text", d != e,
                "this is why it is not the default")

    print("\nforeground package, derived from the dump")
    # Replaces a ~390ms device call with a free read of a dump we already
    # have -- it was 23% of a measured run. The cases that matter are the
    # ones that would send the walker into the wrong app.
    def views_for(pairs):
        return [{"package": pkg} for pkg, n in pairs for _ in range(n)]

    ok &= check("the majority package wins",
                foreground_package(views_for([(PKG, 40), ("com.other", 3)])) == PKG)
    ok &= check("status/nav bar never wins on a sparse screen",
                foreground_package(
                    views_for([("com.android.systemui", 30), (PKG, 2)])) == PKG,
                "systemui is on every screen; excluding it is what makes this work")
    ok &= check("a foreign app in front is reported, not hidden",
                foreground_package(
                    views_for([("com.coloros.gallery3d", 25), (PKG, 4)]))
                == "com.coloros.gallery3d",
                "walking the Gallery as if it were the camera has cost real time")
    ok &= check("the keyboard does not win",
                foreground_package(
                    views_for([("com.google.android.inputmethod.latin", 50),
                               (PKG, 6)])) == PKG)
    ok &= check("no views gives empty, not a crash", foreground_package([]) == "")
    ok &= check("only chrome gives empty, not systemui",
                foreground_package(views_for([("com.android.systemui", 9)])) == "")

    print("\nbounds")
    ok &= check("centre computed", center_of(wrapper) == (1056, 459),
                str(center_of(wrapper)))

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
