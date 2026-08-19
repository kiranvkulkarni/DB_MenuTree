"""Refuse to click controls that do irreversible or outward-facing things.

An exhaustive crawler presses every button it finds. On a personal device
that is a problem: the Samsung camera app reaches a gallery preview with a
Delete button, and Share sends data off the device. Observed on an emulator,
the same behaviour dialled 55 in-call screens in one crawl.

Blocked actions are **reported, not silently dropped**. A control the crawler
declined to press is a known coverage gap, and a gate that hides it would
overstate coverage — the wrong direction to be wrong in.

Matching is case-insensitive against the selector value (the button's text or
content-description), on word boundaries so "Delete" is caught but "Deleted
items" style labels do not accidentally match unrelated words like "complete".
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Irreversible or outward-facing actions, grouped so a caller can see what a
# preset actually covers. Tuned for a first pass on a personal device: it is
# deliberately broad, because a false block costs coverage while a false
# allow can cost data.
GUARD_PRESETS: Dict[str, List[str]] = {
    "destructive": [
        r"delete", r"remove", r"erase", r"discard", r"trash", r"clear all",
        r"format", r"wipe", r"factory", r"reset",
    ],
    "outbound": [
        r"share", r"send", r"upload", r"post", r"publish", r"email",
        r"message", r"call", r"dial", r"invite",
    ],
    "account": [
        r"sign out", r"log out", r"logout", r"unlink", r"disconnect",
        r"deregister", r"delete account", r"switch account",
    ],
    "commerce": [
        r"buy", r"purchase", r"subscribe", r"upgrade", r"payment", r"pay now",
    ],
}

DEFAULT_PRESETS = ("destructive", "outbound", "account", "commerce")


@dataclass
class GuardHit:
    selector_strategy: str
    selector_value: str
    pattern: str
    state: str


@dataclass
class ActionGuard:
    """Decides whether an action may be performed."""

    patterns: List[re.Pattern] = field(default_factory=list)
    enabled: bool = True
    hits: List[GuardHit] = field(default_factory=list)

    @classmethod
    def from_config(
        cls,
        enabled: bool = True,
        presets: Sequence[str] = DEFAULT_PRESETS,
        extra: Optional[Iterable[str]] = None,
    ) -> "ActionGuard":
        raw: List[str] = []
        for name in presets:
            if name not in GUARD_PRESETS:
                raise ValueError(
                    f"unknown guard preset '{name}'; "
                    f"available: {', '.join(sorted(GUARD_PRESETS))}"
                )
            raw.extend(GUARD_PRESETS[name])
        raw.extend(extra or [])

        compiled = [re.compile(rf"\b{p}\b", re.IGNORECASE) for p in raw]
        guard = cls(patterns=compiled, enabled=enabled)
        if enabled:
            logger.info(
                "Action guard ON: %d pattern(s) from preset(s) %s%s",
                len(compiled), ", ".join(presets),
                f" plus {len(list(extra))} custom" if extra else "",
            )
        else:
            logger.warning(
                "Action guard OFF: the crawler may delete data or send it "
                "off the device. Do not use this on a device holding real data."
            )
        return guard

    def blocks(
        self, strategy: str, value: str, state: str = ""
    ) -> Optional[str]:
        """Return the matching pattern if this action must not be performed."""
        if not self.enabled or not value:
            return None
        for pattern in self.patterns:
            if pattern.search(value):
                self.hits.append(
                    GuardHit(strategy, value, pattern.pattern, state)
                )
                return pattern.pattern
        return None

    def summary(self) -> Dict:
        blocked = {}
        for hit in self.hits:
            blocked.setdefault(hit.selector_value, 0)
            blocked[hit.selector_value] += 1
        return {
            "enabled": self.enabled,
            "patterns": len(self.patterns),
            "blocked_attempts": len(self.hits),
            "blocked_controls": sorted(blocked),
        }

    def report(self) -> str:
        if not self.enabled:
            return "Action guard was disabled."
        if not self.hits:
            return "Action guard blocked nothing."
        controls = sorted({h.selector_value for h in self.hits})
        lines = [
            f"Action guard blocked {len(self.hits)} attempt(s) on "
            f"{len(controls)} distinct control(s). These are a KNOWN COVERAGE "
            "GAP, not untested-by-accident:",
        ]
        lines.extend(f"    - {c}" for c in controls[:30])
        if len(controls) > 30:
            lines.append(f"    ... and {len(controls) - 30} more")
        return "\n".join(lines)
