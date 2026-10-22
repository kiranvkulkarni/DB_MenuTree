"""No tap lands on a protected control by accident (agreed 2026-10-20).

    python tests/test_protected_taps.py

Full runs took photos: a prompt drawn over the shutter was answered, and a
tap read from a dump taken just before it closed landed on the shutter. A tap
in a protected control's area is now checked against the screen as it is.
Views are generic; the bounds are the S25 Ultra's (shutter [408,1809][672,2073]).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crawler.element_tree import ElementTreeWalker  # noqa: E402


def check(label, condition, detail=""):
    print(f"  [{'ok' if condition else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))
    return condition


def view(name, bounds, cls="android.widget.Button", children=(), parent=None):
    return {"text": name or None, "content_description": None, "resource_id": None,
            "class": cls, "bounds": "[%d,%d][%d,%d]" % bounds,
            "children": list(children), "parent": parent, "package": "com.example"}


SHUTTER = view("Take picture", (408, 1809, 672, 2073), "android.widget.FrameLayout")
PROMPT_OK = view("Allow", (300, 1900, 780, 2000))          # drawn over the shutter
FILTER = view("Classic film", (674, 1615, 842, 1813))       # beside it


def walker(fresh):
    w = ElementTreeWalker("com.example", None, {"output_dir": "."})
    w.do_not_press = {"take picture"}
    w._capture = lambda with_package=True: (None, fresh, "")
    return w


def main() -> int:
    ok = True
    print("taps away from protected controls need no second look")
    w = walker([])
    ok &= check("a filter beside the shutter", w._tap_is_safe([SHUTTER, FILTER], 1, (758, 1714)))

    print("a prompt over the shutter")
    screen = [SHUTTER, PROMPT_OK]
    ok &= check("still there: tapped", walker(screen)._tap_is_safe(screen, 1, (540, 1950)))
    w = walker([SHUTTER])
    ok &= check("gone since the dump: not tapped",
                not w._tap_is_safe(screen, 1, (540, 1950)))
    ok &= check("counted", w._taps_refused_protected == 1)

    print("a system prompt's dump holds no shutter: where it was last seen counts")
    w = walker([])
    w._tap_is_safe([SHUTTER, FILTER], 1, (758, 1714))        # sees the shutter
    ok &= check("prompt gone, nothing at the point: not tapped",
                not w._tap_is_safe([PROMPT_OK], 0, (540, 1950)))
    w._capture = lambda with_package=True: (None, [PROMPT_OK], "")
    ok &= check("prompt still there: tapped", w._tap_is_safe([PROMPT_OK], 0, (540, 1950)))

    print("the protected control drawn over the target")
    w = walker([PROMPT_OK, SHUTTER])
    ok &= check("not tapped", not w._tap_is_safe([PROMPT_OK, SHUTTER], 0, (540, 1950)))

    print("an unlabelled target is recognised by its bounds")
    bare = view("", (420, 1850, 660, 1950), "android.widget.Switch")
    screen = [SHUTTER, bare]
    ok &= check("tapped", walker(screen)._tap_is_safe(screen, 1, (540, 1900)))

    print("checking does not count as a guard refusal")
    w = walker([])
    w.guard.enabled = True
    before = len(w.guard.hits)
    w._tap_is_safe([view("Delete all", (0, 0, 100, 100))], 0, (50, 50))
    ok &= check("no guard hit recorded", len(w.guard.hits) == before)

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
