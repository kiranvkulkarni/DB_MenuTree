"""The single source of truth for UVTA surface syntax.

Transcribed from the UVTA DSL cheat sheet. Everything here is confirmed by
that document unless explicitly marked otherwise; correct this file and the
whole generator follows, because no other module writes UVTA text.

Selector keywords (cheat sheet, "Selector Types"):

    text          exact text match      click text "Settings"
    textContains  partial text match    click textContains "Set"
    desc          content description   click desc "Shutter"
    id            resource id           click id "btn_submit"
    class         UI class name         click class "Button"
    pkg           package name          click pkg "com.app"
"""
from typing import Optional

from ..parser.menu_tree import Selector

# Selector keywords the DSL accepts. `description`, `resourceid` and
# `className` are listed as accepted aliases; the short forms are used here.
SELECTOR_KEYWORDS = ("text", "textContains", "desc", "id", "class", "pkg")

PRESS_KEYS = ("home", "back", "recents")
CAMERA_TARGETS = ("front", "rear")


def _sel(selector: Selector) -> str:
    return f'{selector.strategy} "{selector.value}"'


# -- UI interaction: click ---------------------------------------------------

def click(selector: Selector) -> str:
    """`click <type> <value>`"""
    return f"click {_sel(selector)}"


def click_xy(x: int, y: int) -> str:
    """`click xy <x>,<y>`"""
    return f"click xy {x},{y}"


def click_relative(direction: str, selector: Selector) -> str:
    """`click <dir> of <type> <value>` -- e.g. `left of text "Menu"`."""
    return f"click {direction} of {_sel(selector)}"


def scroll_click(selector: Selector) -> str:
    """`scroll click <type> <value>` -- scroll until found, then click."""
    return f"scroll click {_sel(selector)}"


def hscroll_click(container: Selector, selector: Selector) -> str:
    """`hscroll <container> click <type> <value>`"""
    return f"hscroll {_sel(container)} click {_sel(selector)}"


# -- UI interaction: verify --------------------------------------------------

def verify_exists(selector: Selector) -> str:
    """`verify <type> <value> exists`"""
    return f"verify {_sel(selector)} exists"


def verify_not_exists(selector: Selector) -> str:
    """`verify <type> <value> !exists`"""
    return f"verify {_sel(selector)} !exists"


# -- device control ----------------------------------------------------------

def press(key: str) -> str:
    """`press home|back|recents`"""
    key = key.lower()
    if key not in PRESS_KEYS:
        raise ValueError(f"unsupported press key {key!r}; expected {PRESS_KEYS}")
    return f"press {key}"


def switch_camera(target: str) -> str:
    """`switch_camera "front"|"rear"`"""
    target = target.lower()
    if target not in CAMERA_TARGETS:
        raise ValueError(
            f"unsupported camera target {target!r}; expected {CAMERA_TARGETS}"
        )
    return f'switch_camera "{target}"'


# -- media -------------------------------------------------------------------

def get_media(mode: str, media_type: str, seconds: Optional[int] = None) -> str:
    """`get captured|preview image|video [<n>]`"""
    if mode not in ("captured", "preview"):
        raise ValueError(f"mode must be captured|preview, got {mode!r}")
    if media_type not in ("image", "video"):
        raise ValueError(f"type must be image|video, got {media_type!r}")
    command = f"get {mode} {media_type}"
    return f"{command} {seconds}" if seconds is not None else command


# -- control flow ------------------------------------------------------------

def repeat(times: int) -> str:
    """`repeat <N>`"""
    return f"repeat {times}"


def repeat_range(variable: str, start: int, end: int) -> str:
    """`repeat <v> from <X> to <Y>`"""
    return f"repeat {variable} from {start} to {end}"


def repeat_until(condition: str) -> str:
    """`repeat until <cond>` -- e.g. `repeat until exists "Done"`."""
    return f"repeat until {condition}"


# -- conditional execution ---------------------------------------------------

def if_(command: str, condition: str) -> str:
    """`<cmd> if <cond>` -- e.g. `click desc "X" if exists`."""
    return f"{command} if {condition}"


def unless(command: str, condition: str) -> str:
    """`<cmd> unless <cond>`"""
    return f"{command} unless {condition}"


# -- suite structure ---------------------------------------------------------

def testcase_header(name: str) -> str:
    return f"TESTCASE: {name}"


# -- NOT on the cheat sheet --------------------------------------------------
# Kept because the generator needs them, but treat as unconfirmed.

def launch(package: str) -> str:
    """`launch "<package>"`

    UNCONFIRMED. The cheat sheet has no app-lifecycle section, yet every
    generated test case opens with this. If the DSL spells it differently --
    `start`, `open`, `launch_app` -- or if the runner launches the app itself
    and no command is needed, this one function is the only thing to change.
    """
    return f'launch "{package}"'


def long_click(selector: Selector) -> str:
    """UNCONFIRMED: no long-press variant appears on the cheat sheet."""
    return f"long_click {_sel(selector)}"


def set_text(selector: Selector, text: str) -> str:
    """UNCONFIRMED: no text-entry command appears on the cheat sheet."""
    escaped = text.replace('"', '\\"')
    return f'input {_sel(selector)} text "{escaped}"'
