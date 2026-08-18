"""The single source of truth for UVTA surface syntax.

IMPORTANT: only the `launch` / `click` / `verify ... exists timeout` forms are
confirmed -- they are taken from the worked example in README section 4B.
Everything else here (back, input, long-press, scroll) is inferred and marked
UNVERIFIED. Correct the constants in this one file and the whole generator
follows; no other module hardcodes UVTA text.
"""
from typing import Optional

from ..parser.menu_tree import Selector

DEFAULT_VERIFY_TIMEOUT = 2.0


def launch(package: str) -> str:
    return f'launch "{package}"'


def click(selector: Selector) -> str:
    return f'click {selector.strategy} "{selector.value}"'


def verify_exists(selector: Selector, timeout: float = DEFAULT_VERIFY_TIMEOUT) -> str:
    return f'verify {selector.strategy} "{selector.value}" exists timeout {timeout}'


# --- UNVERIFIED forms -------------------------------------------------------
# Confirm these against the real UVTA grammar before trusting a gate result.

def long_click(selector: Selector) -> str:
    return f'long_click {selector.strategy} "{selector.value}"'  # UNVERIFIED


def set_text(selector: Selector, text: str) -> str:
    escaped = text.replace('"', '\\"')
    return f'input {selector.strategy} "{selector.value}" text "{escaped}"'  # UNVERIFIED


def press_key(key_name: str) -> str:
    return f"press {key_name.lower()}"  # UNVERIFIED


def scroll(selector: Optional[Selector], direction: str) -> str:
    if selector is None:
        return f"scroll {direction.lower()}"  # UNVERIFIED
    return f'scroll {selector.strategy} "{selector.value}" {direction.lower()}'  # UNVERIFIED


def testcase_header(name: str) -> str:
    return f"TESTCASE: {name}"
