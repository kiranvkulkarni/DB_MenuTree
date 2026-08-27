"""Match a hand-written spec label to an element on screen.

**The depth columns are not selectors.** They are how a manual test engineer
described the element, in their own English, while looking at the phone. The
on-screen text is often something else entirely:

    spec label                      what the screen actually says
    ------------------------------  ------------------------------
    "Back key icon"                 content-desc "Navigate up"
    "Flash icon"                    content-desc "Flash"
    "Priorize quality"              "Prioritize quality"      (typo)
    "JEPG format"                   "JPEG"                    (typo)
    "High efficiency pitures"       "High efficiency pictures"(typo)
    "Scene Optimiser"               "Scene optimizer"         (spelling)
    "Location tags ...recorded."    a long sentence, elided
    "Watermark(On/Off)"             "Watermark"               (annotation)

Exact matching, or exact-then-substring, fails on most of these and reports
a Fail on a control that is present and working. For a release gate that is
the worst possible error: it manufactures defects, and a gate that cries wolf
gets ignored.

So matching is deliberately tolerant, and every match carries a **score and a
reason**. A confident match is treated as found; a weak one is still reported
as found but flagged for review, because a human reading the workbook can
settle in two seconds what no heuristic should decide silently.

The durable fix is not a cleverer heuristic -- it is to record the label to
selector mapping once a human has confirmed it. `proposed_aliases()` writes
that file; `--aliases` reads it back, and an alias always wins over a guess.
"""
import difflib
import re
from typing import Dict, List, Optional, Sequence, Tuple

# Words that describe a control rather than name it. A tester writes "Back key
# icon"; the screen says "Navigate up". Dropping these lets the real words
# meet.
NOISE_WORDS = {
    "icon", "button", "btn", "key", "tab", "menu", "option", "options",
    "item", "toggle", "switch", "soft", "softkey", "image", "img",
    "the", "a", "an", "of", "to", "for", "and", "or",
}

# Annotations the sheet appends for readers, beyond those spec_reader strips.
TRAILING_NOTE = re.compile(r"\s*\((?:on|off|on\s*/\s*off|enabled|disabled)\)\s*$",
                           re.IGNORECASE)

# A label the author elided rather than transcribing in full.
ELLIPSIS = re.compile(r"\.\.\.|…")

_PUNCT = re.compile(r"[^\w\s.]+")
# A dot with no digit on either side is punctuation; one against a
# digit is part of the number.
_LONE_DOT = re.compile(r"(?<![0-9])\.(?![0-9])")
_SPACES = re.compile(r"\s+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# A number, optionally followed by a unit that is written
# inconsistently between the sheet and the screen.
_QUANTITY = re.compile(r"^(\d*\.?\d+)\s*(x|sec|secs|s)?$", re.IGNORECASE)


def normalise(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    if not text:
        return ""
    text = TRAILING_NOTE.sub(" ", text)
    # Underscore is a word character, so _PUNCT leaves it alone and
    # "FRONT_TIMER_OFF" stays a single token. That scored 0.33 against
    # "Timer off" instead of 0.85, because the two never got to compare word
    # by word. Some controls expose an internal-style label rather than
    # display text, and those are exactly the ones with no other handle.
    text = text.replace("_", " ")
    # Keep a decimal point that touches a digit. Stripping it turned "0.6x"
    # into the two tokens 0 and 6, which then "contained" the 6 of "6x" --
    # scoring 0.88 between two different zoom levels.
    text = _LONE_DOT.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip().lower()


def canonical_number(word: str) -> str:
    """Fold a quantity written two ways into one form.

    The sheet and the screen agree on the value and disagree on the notation:

        sheet     device     both mean
        "0.6x"    ".6"       0.6
        "1x"      "1"        1
        "2sec"    "2S"       2 seconds

    20 rows differed only by a trailing "x" on a zoom level and 5 more by
    sec/s on a timer, all reported as missing controls. Comparing the number
    numerically fixes them without loosening anything else: "1x" and "15x"
    stay 1 and 15, and "12M" keeps its M so it cannot meet "50M".
    """
    match = _QUANTITY.match(word)
    if not match:
        return word
    value, unit = match.group(1), (match.group(2) or "").lower()
    try:
        number = format(float(value), "g")
    except ValueError:
        return word
    if unit in ("sec", "secs", "s"):
        return number + "s"
    return number          # bare, or a zoom "x" which carries no meaning


def stem(word: str) -> str:
    """Fold a trailing plural so token sets can meet.

    A tester writes "Quick settings"; the control's id is
    `quick_setting_entry_button`. One character kept those apart and the whole
    Settings sheet failed on it -- every row lived under that first step, so
    80 of 88 rows reported Fail against controls that were present and
    working.

    Deliberately minimal: only a trailing "s", and only on words long enough
    that it is unlikely to be the whole word. Anything cleverer risks folding
    words that genuinely differ, and a false match hides a defect.
    """
    if (len(word) > 3 and word.endswith("s")
            and not word.endswith("ss") and not word.endswith("us")):
        return word[:-1]
    return word


def tokens(text: str, drop_noise: bool = True) -> List[str]:
    words = normalise(text).split()
    if not drop_noise:
        return words
    kept = [w for w in words if w not in NOISE_WORDS]
    # Never reduce a label to nothing: "Back key icon" is all noise words,
    # and an empty token set matches everything.
    return [stem(canonical_number(w)) for w in (kept or words)]


def from_resource_id(resource_id: Optional[str]) -> str:
    """`com.x:id/flash_auto_button` -> `flash auto button`.

    Resource ids are developer English, which is often closer to the tester's
    English than the visible text is -- especially for icons, which have no
    text at all.
    """
    if not resource_id:
        return ""
    tail = resource_id.split("/")[-1]
    tail = _CAMEL.sub(" ", tail)
    return _SPACES.sub(" ", tail.replace("_", " ").replace("-", " ")).strip().lower()


def _contains(inner: Sequence[str], outer: Sequence[str]) -> bool:
    """Is `inner` a contiguous run of whole words inside `outer`?"""
    if not inner or len(inner) > len(outer):
        return False
    span = len(inner)
    return any(list(outer[i:i + span]) == list(inner)
               for i in range(len(outer) - span + 1))


def score(spec_label: str, candidate: str) -> Tuple[float, str]:
    """How well `candidate` answers `spec_label`, and why. 0.0 to 1.0."""
    if not spec_label or not candidate:
        return 0.0, "empty"

    a, b = normalise(spec_label), normalise(candidate)
    if not a or not b:
        return 0.0, "empty after normalising"
    if a == b:
        return 1.0, "exact"

    ta, tb = tokens(spec_label), tokens(candidate)

    # A number is exact. "1x" and "15x" are textually similar -- SequenceMatcher
    # scores them 0.80 -- but they are different zoom levels, and treating one
    # as the other is a false Pass on a control nobody checked.
    if (len(ta) == 1 and len(tb) == 1
            and _QUANTITY.match(ta[0]) and _QUANTITY.match(tb[0])
            and ta[0] != tb[0]):
        return 0.05, "different quantities"

    if ta == tb:
        return 0.97, "same words"
    if set(ta) == set(tb):
        return 0.95, "same words, different order"

    # The author elided the middle of a long label: compare what they wrote.
    if ELLIPSIS.search(spec_label):
        head = normalise(ELLIPSIS.split(spec_label)[0])
        if head and b.startswith(head):
            return 0.92, "matches the part before the ellipsis"

    # Containment must align on WORD boundaries, not raw characters.
    #
    # Raw substring matching produced false passes, which are worse than
    # false failures: a false Fail is investigated, a false Pass hides a
    # defect. Measured on the Pro branch -- "On" matched "Exposure monitor"
    # (on is inside m-on-itor) and "Pro [Tittle]" matched a lone "T". Both
    # were reported as Pass against controls nobody had checked.
    if _contains(ta, tb) or _contains(tb, ta):
        return 0.88, "one contains the other"

    sa, sb = set(ta), set(tb)
    if sa and sa <= sb:
        return 0.85, "every spec word appears on screen"
    if sb and sb <= sa:
        return 0.82, "screen text is a subset of the spec wording"

    overlap = len(sa & sb)
    if overlap:
        shared = overlap / max(len(sa), len(sb))
        if shared >= 0.5:
            # Typos and spelling variants: "Optimiser" vs "optimizer".
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            return max(shared * 0.8, ratio * 0.8), f"{overlap} word(s) in common"

    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio >= 0.75:
        return ratio * 0.8, f"similar spelling ({ratio:.2f})"
    return ratio * 0.5, "weak"


class Match:
    """One candidate, scored."""

    __slots__ = ("element", "score", "why", "matched_on")

    def __init__(self, element, score: float, why: str, matched_on: str):
        self.element = element
        self.score = score
        self.why = why
        self.matched_on = matched_on

    def __repr__(self) -> str:
        return (f"Match({getattr(self.element, 'label', '?')!r}, "
                f"{self.score:.2f}, {self.why})")


def best_match(spec_label: str,
               elements: Sequence,
               aliases: Optional[Dict[str, str]] = None) -> Optional[Match]:
    """The element that best answers this spec row, or None.

    A confirmed alias wins outright. Otherwise every element is scored on its
    visible label *and* on its resource id, and the best wins.
    """
    if not spec_label or not elements:
        return None

    if aliases:
        wanted = aliases.get(spec_label) or aliases.get(normalise(spec_label))
        if wanted:
            if isinstance(wanted, str):          # tolerate an older caller
                wanted = [wanted]
            targets = {normalise(w) for w in wanted}
            # Visible label first, then the resource id: a control the sheet
            # names by its value is often an icon whose only handle is an id.
            for source in ("label", "resource-id"):
                for element in elements:
                    text = (element.label if source == "label"
                            else from_resource_id(getattr(element, "resource_id", None)))
                    if text and normalise(text) in targets:
                        return Match(element, 1.0, "confirmed alias", "alias")

    # A resource id shared by several elements on one screen is a CLASS name,
    # not an instance name, so it cannot identify which one is meant.
    #
    # This cost a real misclick: the Samsung camera gives Flash, Resolution,
    # Motion photo and Filters the same id, `quick_setting_button_main`.
    # Matching "Quick settings" against that id scored 0.765 -- exactly the
    # same as the genuine target, `quick_setting_entry_button` on the "Quick
    # controls" button -- and the tie was broken by document order, so the
    # walk pressed Flash. Every row below then failed against a control that
    # was present and working.
    shared: Dict[str, int] = {}
    for element in elements:
        rid = from_resource_id(getattr(element, "resource_id", None))
        if rid:
            shared[rid] = shared.get(rid, 0) + 1

    best: Optional[Match] = None
    for element in elements:
        rid = from_resource_id(getattr(element, "resource_id", None))
        sources = [("label", element.label)]
        if rid and shared.get(rid, 0) == 1:
            sources.append(("resource-id", rid))
        for source, text in sources:
            if not text:
                continue
            value, why = score(spec_label, text)
            # A resource-id match is weaker evidence than visible text.
            if source == "resource-id":
                value *= 0.9
            # Strictly greater, so a tie keeps the earlier candidate -- and
            # because label is scored before resource-id for each element,
            # visible text wins a tie against an id.
            if best is None or value > best.score:
                best = Match(element, value, why, source)
    return best


# A match at or above this is treated as found without comment.
CONFIDENT = 0.80
# Between REVIEW and CONFIDENT: treated as found, but flagged in the workbook.
# Below REVIEW: not found.
REVIEW = 0.60


def load_aliases(path) -> Dict[str, List[str]]:
    """Read a confirmed spec-label -> on-screen-text map.

    A label maps to a *list* of acceptable targets, because the same sheet
    wording is a different string on different screens. The camera writes the
    12 megapixel option as `BACK_CAMERA_PICTURE_SIZE_NORMAL` in Photo mode and
    `BACK_CAMERA_PRO_PICTURE_SIZE_NORMAL` in Pro; both are "12M" to the author
    of the sheet, so one global alias cannot serve both. Any target matching
    is a match -- these are alternatives, not a conjunction.

    Accepts {"spec": "text"}, {"spec": ["a", "b"]}, or the review file's
    richer form, so a reviewed file can be fed straight back in.
    """
    import json
    from pathlib import Path

    def as_list(value) -> List[str]:
        if isinstance(value, str):
            return [value]
        return [v for v in value if isinstance(v, str) and v]

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: Dict[str, List[str]] = {}
    for key, value in (data.get("aliases", data) or {}).items():
        if isinstance(value, (str, list)):
            targets = as_list(value)
        elif isinstance(value, dict) and value.get("on_screen"):
            # Only honour entries a human marked confirmed.
            if not value.get("confirmed", True):
                continue
            targets = as_list(value["on_screen"])
        else:
            continue
        if targets:
            out[key] = targets
    return out


def proposed_aliases(rows: Sequence[Dict]) -> Dict:
    """Everything matched by guesswork, for a human to confirm once.

    This is the durable fix for hand-written labels. No heuristic should be
    permanently responsible for deciding that "Priorize quality" means
    "Prioritize quality" -- a person confirms it once, and from then on the
    match is exact and auditable.

    Set `confirmed: false` on anything wrong; those entries are ignored on
    the next run rather than silently trusted.
    """
    aliases: Dict[str, Dict] = {}
    for row in rows:
        if row.get("score") is None or row["score"] >= 1.0:
            continue
        spec = row["spec_label"]
        if spec in aliases and aliases[spec]["score"] >= row["score"]:
            continue
        aliases[spec] = {
            "on_screen": row["on_screen"],
            "score": round(row["score"], 3),
            "why": row.get("why", ""),
            "matched_on": row.get("matched_on", ""),
            "sheet_row": row.get("sheet_row", ""),
            "confirmed": row["score"] >= CONFIDENT,
        }
    return {
        "_README": [
            "Confirmed mappings from the sheet's wording to what the screen",
            "actually says. Review each entry, set confirmed:false on any that",
            "are wrong, then pass this file with --aliases on the next run.",
            "A confirmed alias always beats a guess.",
        ],
        "aliases": dict(sorted(aliases.items())),
    }
