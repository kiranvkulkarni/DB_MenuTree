"""Depth-first descent that never leaves the place it is working.

The element-tree walker keeps a global worklist and, for each item, navigates
to a remembered screen by replaying a path from the root. Two whole categories
of failure come from that and from nothing else:

* **unreachable** -- the replay did not land where the recorded path said.
* **needs a precondition** -- the screen was enumerated once, minutes ago, and
  the walk is comparing against a snapshot that has since gone stale.

Neither is a fact about the app. They are artefacts of jumping.

This walker follows the procedure a manual tester actually uses. It is always
physically standing on the screen it is working on:

    list this screen
    for each item, in order:
        press it
        if it opened something, walk that, then step back ONE level
    when the screen is exhausted, return to the parent and continue there

Because it never jumps, there is nothing to be unreachable *from*, and because
it re-reads the screen every time it comes back, there is no stale snapshot to
be wrong about. Rows are emitted as they are visited, so the sheet comes out
in tree order with depth equal to recursion depth -- no reshaping afterwards.

Three rules were confirmed against the hand-authored S25 Ultra MenuTree:

* **Options are listed, never pressed.** `12M / 50M / 200M` are leaves in the
  sheet. Pressing one sets the camera to 200MP, which changes every screen
  after it.
* **Getting back prefers the screen's own control.** BACK on a viewfinder
  exits the camera.
* **Modes are siblings, not children.** To leave VIDEO you press PHOTO. The
  dump marks the current one `selected`, which is how the parent is found.
"""
import logging
import re
import time
from typing import Dict, List, Optional, Sequence, Set

from .element_tree import ElementTreeWalker, TreeNode
from .device_driver import DriverError
from .elements import Element, _pressable_via, label_set, screen_similarity
from .elements import CHROME_PACKAGES
from .hierarchy import parse_hierarchy
from .hierarchy import box_of, center_of, looks_like_dialog, scrollable_container

logger = logging.getLogger(__name__)


class Node:
    """One screen in the tree the walk is building.

    The walk keeps the tree it has already built and navigates by consulting
    it, rather than by re-deriving where things are. The field that matters is
    `entering`: the element that was pressed to arrive here.

    That single fact answers "how do I get to this node" uniformly:

      * a MODE is entered by pressing its tab, and the tab is still on screen
        from inside a sibling mode -- so leaving VIDEO for Photo is just
        pressing Photo's own entering element. No `selected` flag needed,
        which matters because this camera does not set one on the modes.
      * a SUBMENU is entered by pressing a menu item that its own screen does
        not show, so the lookup finds nothing and the walk falls back to the
        screen's back control.

    Same rule, two behaviours, and the right one each time.
    """

    __slots__ = ("label", "depth", "path", "path_selectors", "signature",
                 "elements", "entering", "parent", "enabled", "own", "list_only",
                 "inherited")

    def __init__(self, label, depth, path, path_selectors, elements,
                 signature, entering=None, parent=None):
        self.label = label
        self.depth = depth
        self.path = list(path)
        self.path_selectors = list(path_selectors)
        self.elements = list(elements)
        self.signature = signature
        self.entering = entering
        self.parent = parent
        self.enabled = {}           # fingerprint -> enabled, from the sweep
        self.own = set()            # fingerprints not carried over from above
        self.list_only = False      # profile: list what is here, press nothing
        self.inherited = set()      # fingerprints carried over from above

    @property
    def where(self):
        return " > ".join(self.path[-2:]) or "<root>"

# Controls that leave a screen. Checked against the label, lowercased.
BACK_LABELS = ("navigate up", "back", "close", "cancel", "done", "up")



class RecursiveWalker(ElementTreeWalker):
    """Walks the tree the way the sheet is written."""

    def __init__(self, package: str, serial: Optional[str], config: Dict):
        super().__init__(package, serial, config)
        self._nodes_visited = 0
        self._returns_ok = 0
        self._returns_failed = 0
        self._returned_by: Dict[str, int] = {}
        self._options_listed = 0
        self._panels_listed = 0
        self._popup_presses = 0
        self._main_labels = set()   # the app's main screen, from the first launch
        self._states_reset = 0
        self._titles_not_pressed = 0
        self._selectors_walked = 0
        self._clipped_rows_swept = 0
        self._selectors_not_swiped = 0
        self._selections_restored = 0
        self._captions_listed = 0
        self._presses_undone = 0
        self._loops_refused = 0
        self._reused_screens = 0
        self._strips_not_swiped = 0
        self._left_unopened = 0
        self._actions_not_pressed = 0
        self._toggles_not_pressed = 0
        self._recovered = 0
        self._not_located = 0
        self._roles = {}
        self._switches_already_on = 0
        self._switches_revealing = 0
        self._switch_dialogs = 0
        self._switches_navigating = 0
        self._guarded_rows_opened = 0
        self._replay_retaps = 0
        self._switches_restored = 0
        self._switches_not_restored = 0
        self._reopened = 0
        self._documented = {}
        # The tab strip is drawn on EVERY screen the app has. Once its members
        # are nodes at depth 2 they must never be entered again from anywhere
        # else, or the walk re-enters a mode from inside another mode:
        # measured as `PORTRAIT > Quick controls > PHOTO > Filters > Take
        # picture > PHOTO > Quick controls > VIDEO > ...`, max_depth 19, and
        # 483 of 603 rows carrying a path that repeated a label.
        #
        # This is also what "a common menu is covered once" means for tabs:
        # they are documented where they belong, at the top, and are chrome
        # everywhere else.
        self._tab_labels = set()
        # Deliberately far above return_similarity: this decides "is this the
        # SAME menu I already wrote down", not "did I land where I meant".
        self.documented_similarity = float(config.get("documented_similarity", 0.9))
        # Off by default. Suppressing a screen because something like it was
        # already written down is too blunt: the Flash options reached through
        # `Quick controls` are the SAME set as those under the mode's own
        # Flash chip, so skipping them left `Quick controls > Flash` with no
        # children at all. A control reachable from two places is listed under
        # both; the loop guards, not this, are what stop a walk running on
        # forever.
        # Share of the visible labels that must belong to a node for the
        # walk to believe it is standing on it. See _is_node.
        self.node_containment = float(config.get("node_containment", 0.7))
        # Profile opt-in: open a guarded ROW (e.g. "Reset settings") to list
        # the confirmation it raises; the dialog's commit is still refused.
        self.open_guarded_rows = bool(config.get("open_guarded_rows", False))
        # path (as a tuple, ending in the synthetic "On") -> (switch element,
        # its resource id, wanted state). Lets a replay pass through a switch.
        self._switch_steps = {}
        self._replayed_last = None
        self._in_dialog = False
        self.do_not_press = {str(x).strip().lower()
                             for x in config.get("do_not_press", [])}
        # From the profile: controls that switch the app into a parallel
        # state (a second camera). Listed, never pressed here; walked as a
        # pass of their own.
        self.walk_separately = {str(x).strip().lower()
                                for x in config.get("walk_separately", [])}
        self._separate_labels = [str(x).strip() for x in config.get("walk_separately", [])]
        # From the profile. first_launches: how many launches after clearing
        # the app show first-run pop-ups (the camera asks about Location tags
        # on the third); recorded with every answer branch. launch_answers:
        # the answers that set the state the main walk runs in (agreed:
        # Location granted). suggestions: hints the app shows by conditions
        # ("Use Night mode" in dim light) -- listed, never pressed.
        self.first_launches = int(config.get("first_launches", 0) or 0)
        self.launch_answers = [str(x) for x in config.get("launch_answers", [])]
        self.suggestions = {str(x).strip().lower() for x in config.get("suggestions", [])}
        # From the profile: rows that lead out of the app to somewhere that is
        # not part of its menu tree (a store). Listed, not opened.
        self.leaves_app = {str(x).strip().lower()
                           for x in config.get("leaves_app", [])}
        # A screen reached again by another route -- Settings, through Quick
        # controls in every mode. full: walk it fully every time. top: list
        # its rows again, but do not reopen a row it already had where it was
        # walked before (rows new here are opened). note: one row saying where
        # it was listed. Only near-identical screens count as "the same".
        self.revisits = str(config.get("revisits", "top"))
        if self.revisits not in ("full", "top", "note"):
            raise ValueError(f"revisits must be full, top or note, not {self.revisits!r}")
        self.revisit_similarity = float(config.get("revisit_similarity", 0.9))
        self._walked = []           # (path, signature) of every screen walked
        self._pass_marker = None    # the control that started the current pass
        self._same_as_main = set()  # pass paths whose screen is the main walk's
        self._passes_walked = 0
        self._opens_app = {}        # control label -> (package, path listed under)
        self._revisited = 0
        # From the profile: rows opened to LIST what they show, nothing on
        # that screen pressed, left by Back (agreed: MORE > Edit, where a
        # press could rearrange the mode bar).
        self.list_only = {str(x).strip().lower()
                          for x in config.get("list_only", [])}
        self.skip_documented_screens = bool(
            config.get("skip_documented_screens", False))

    # -- helpers ---------------------------------------------------------
    def _signature(self, elements: Sequence[Element]) -> Set[tuple]:
        return {self._fingerprint(e) for e in elements}

    def _is_back(self, element: Element) -> bool:
        if element.kind == "back" or element.label.strip().lower() in BACK_LABELS:
            return True
        # An unlabelled control named only by its resource id: the front
        # camera's Color tone panel closes by `close_button`. The same back
        # words, read from the id.
        rid = (element.selector_value or "") if element.selector_kind == "id" else ""
        if rid and element.label == rid.split("/")[-1]:
            words = re.split(r"[_\W]+", element.label.lower())
            return any(w in BACK_LABELS for w in words if w)
        return False

    def _worth_pressing(self, element: Element) -> Optional[str]:
        """None if it should be pressed, else why it should not be.

        Being un-clickable in the dump is NOT a reason to skip a row. A menu
        item is whatever a tester would tap, and Android hangs the click on a
        container while the text you can see reports `clickable="false"`.
        Reading that flag and believing it left every Settings row unpressed
        and stopped the tree four levels short of the hand-authored sheet.

        Pressing something inert costs one tap and changes nothing. Refusing
        to press something live costs a whole subtree, silently. The
        asymmetry decides it.

        The real vetoes stay: a back control leaves the screen, a keypad key
        can dial, and the action guard refuses destructive, outbound,
        account and commerce controls.
        """
        if self._is_back(element):
            return "back control"

        if element.label.strip().lower() in self.do_not_press:
            # From the app's profile (profiles/<package>.json), never from
            # this file. The walker knows nothing about any particular app;
            # what a given app must not have pressed -- a shutter that saves
            # a photo on every tap -- is data maintained beside that app.
            self._actions_not_pressed += 1
            return "listed, not pressed (profile: do_not_press)"
        if element.label.strip().lower() in self.walk_separately:
            return "listed, not pressed (profile: walked as its own pass)"
        if element.label.strip().lower() in self.leaves_app:
            return "listed, not opened (profile: leads out of the app)"
        if element.label.strip().lower() in self.suggestions:
            return "suggestion shown by conditions -- listed, not pressed (profile)"
        from .element_tree import KEYPAD_KEY
        if element.role == "option":
            return "option -- listed, not selected"
        if KEYPAD_KEY.fullmatch(element.label.strip()):
            self._keypad_skipped += 1
            return "keypad key -- recorded, not pressed"
        # In a dialog every button is checked strictly -- that is where a
        # commit like `Reset` or `Yes, delete` lives. Elsewhere a row is an
        # action only if it STARTS with the verb (see ActionGuard.blocks_row).
        if self._in_dialog:
            blocked = self.guard.blocks("text", element.label)
        else:
            blocked = self.guard.blocks_row("text", element.label)
            if blocked and self.open_guarded_rows and element.role == "title":
                # Opted into by the app's profile: open the row to document
                # the confirmation it raises. Inside that dialog the strict
                # check above refuses the destructive button, and the walk
                # leaves by Cancel.
                self._guarded_rows_opened += 1
                return None
        if blocked:
            return f"action guard: {blocked}"
        return None

    def _emit(self, element: Element, depth: int, path: List[str],
              path_selectors: List[tuple], note: str = "") -> TreeNode:
        # A text block of several paragraphs is several rows, as the sheet
        # writes it: Log's explanation is one TextView, and the sheet has
        # "Log videos will look flat and grey..." and "Select which modes..."
        # on rows of their own. Only plain text is split -- a control keeps
        # its label whole, because that label is how it is found again.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", element.label or "")
                      if p.strip()]
        if len(paragraphs) > 1 and not element.interactive:
            row = None
            for para in paragraphs:
                part = Element(label=para, kind=element.kind, interactive=False,
                               view_index=element.view_index)
                row = self._emit_one(part, depth, path, path_selectors, note)
            return row
        return self._emit_one(element, depth, path, path_selectors, note)

    def _emit_one(self, element: Element, depth: int, path: List[str],
                  path_selectors: List[tuple], note: str = "") -> TreeNode:
        row = TreeNode(
            label=element.annotated(),
            raw_label=element.label,
            kind=element.kind,
            depth=depth,
            path=list(path),
            path_selectors=list(path_selectors),
            interactive=element.interactive,
            blocked=self.guard.blocks("text", element.label) if element.interactive else None,
            selector_kind=element.selector_kind,
            selector_value=element.selector_value,
            note=note,
        )
        self.rows.append(row)
        if len(self.rows) % self.checkpoint_every == 0:
            self._checkpoint()
        return row

    # -- getting back ----------------------------------------------------
    def _return_to(self, node: "Node") -> bool:
        """Get back to `node`, using the tree rather than guesswork.

        Ordered by how safe each move is, not by how obvious:

            1. the element that ENTERED that node, if it is on screen
            2. the screen's own back control  (Navigate up, Close)
            3. the hardware BACK key
            4. relaunch and replay the path

        Step 1 is what makes modes work and is the reason a node records what
        opened it. Pressing a mode tab from a sibling mode goes exactly where
        the tree says it goes. For a submenu the entering item is not on the
        submenu's own screen, so the lookup simply finds nothing and the walk
        moves on to the back control -- one rule, the right behaviour in both
        cases.

        The hardware key is third because on a camera viewfinder it leaves the
        app entirely. A walk on the launcher has lost its place completely; a
        wrong tap has only lost a screen.
        """
        def arrived() -> bool:
            _, views, package = self._await_stable()
            if not views or (package and package != self.package):
                return False
            return self._is_node(node.elements, self._elements(views), node.own)

        def press(element: Element, views, how: str) -> bool:
            if self.guard.blocks("text", element.label):
                return False
            if not self._click(element, views):
                return False
            # Let the press land: a panel read while it slides open is not
            # yet the panel, and the return was counted a failure.
            self._after_press(label_set(self._elements(views)))
            if arrived():
                self._returns_ok += 1
                self._returned_by[how] = self._returned_by.get(how, 0) + 1
                return True
            return False

        def via_entering() -> bool:
            # The control that opens this screen, if it is in view now.
            # Checked after EVERY step back, not only first: Timer's options
            # and the Gallery both drop the walk on the camera screen, where
            # `Quick controls` is one press from home.
            if node.entering is None:
                return False
            _, views, _ = self._await_stable()
            wanted = self._fingerprint(node.entering)
            live = next((e for e in self._elements(views or [])
                         if self._fingerprint(e) == wanted), None)
            return live is not None and press(live, views, "the control that opens it")

        def hardware_back() -> bool:
            try:
                self.driver.press_back()
                time.sleep(self.settle)
            except Exception:
                return False
            if arrived():
                self._returns_ok += 1
                self._returned_by["BACK"] = self._returned_by.get("BACK", 0) + 1
                return True
            return via_entering()

        def walk_back() -> bool:
            # Before the hardware key (which on a camera screen closes the
            # app) and before a relaunch (which restarts, here wipes, it): if
            # the walk is still in the app and the path's first tab is in
            # view, walk the path from here. A MORE mode's own Back lands on
            # PHOTO, two presses (MORE, the tile) from home.
            _, views, package = self._await_stable()
            if not (package == self.package and node.path
                    and node.path[0].strip().lower() in self._tab_labels
                    and self._find_element(node.path[0], views or []) is not None):
                return False
            if self._replay(node.path, launch=False) and arrived():
                self._returns_ok += 1
                self._returned_by["walked back"] = self._returned_by.get("walked back", 0) + 1
                return True
            return False

        if via_entering():
            return True

        # In another app, its own `Navigate up` moves around inside THAT app
        # (the Gallery's went to an album). The system Back leaves it.
        _, views, package = self._await_stable()
        if package and package != self.package:
            for _ in range(2):
                if hardware_back():
                    return True
                _, views, package = self._await_stable()
                if package == self.package:
                    break

        _, views, _ = self._await_stable()
        here = self._elements(views) if views else []
        for element in here:
            # Not a back control carried over from a screen above: FOOD's own
            # `Back` stays in view while its Quick controls' Ratio options are
            # open, and pressing it to return to Quick controls left FOOD.
            if self._is_back(element) and self._fingerprint(element) not in node.inherited:
                if press(element, views, "on-screen back") or via_entering():
                    return True
                break

        if walk_back() or hardware_back() or walk_back():
            return True

        _, views, package = self._await_stable()
        logger.info("return to %s needs a relaunch; on screen (%s): %s",
                    node.where, package,
                    [e.label for e in self._elements(views or [])][:10])
        # Last resort: relaunch and walk back down the path. `_replay`
        # scrolls to each step; the older replay did not, and verified its
        # landing against an empty state key, so it could never succeed.
        if self._replay(node.path):
            if arrived():
                self._returns_ok += 1
                self._returned_by["relaunch"] = self._returned_by.get("relaunch", 0) + 1
                return True

        self._returns_failed += 1
        return False

    # -- structure -------------------------------------------------------
    def _tab_strip(self, elements: Sequence[Element], views) -> List[Element]:
        """A row of sibling tabs, or [].

        The benchmark sheet makes the mode a NODE -- `Photo` at depth 2, its
        controls at depth 3 -- rather than listing the viewfinder controls
        directly under the app. That is not cosmetic: it is what gives the
        walk a way back, because a mode is left by pressing a sibling mode.

        Two signals, and the second was learned the hard way:

        * **a shared resource id with differing labels** -- four controls
          called PORTRAIT / PHOTO / VIDEO / MORE all answering to
          `shooting_mode_item_button` is a tab group.
        * **a real widget class, not a layout.** Without this the rule picked
          the largest group instead, which was the six zoom lenses
          (`RelativeLayout`), and the whole tree came out rooted in
          `Ultra wide lens` at depth 2 with the modes buried at depth 3.
          The quick settings (`FrameLayout`) are the same trap. A tab is a
          control; a container full of controls is not.

        Deliberately narrow. If nothing qualifies the walk simply treats these
        as ordinary elements and descends into them normally -- a flatter tree
        than the sheet wants, but never a wrong one.
        """
        def widget_class(element: Element) -> str:
            if 0 <= element.view_index < len(views):
                return (views[element.view_index].get("class") or "").split(".")[-1]
            return ""

        by_id: Dict[str, List[Element]] = {}
        for element in elements:
            if element.interactive and element.resource_id:
                by_id.setdefault(element.resource_id, []).append(element)

        best: List[Element] = []
        for group in by_id.values():
            labels = {e.label.strip().lower() for e in group if e.label.strip()}
            if len(labels) < 2 or len(labels) != len(group):
                continue
            if not all(widget_class(e).endswith("Button") for e in group):
                continue
            if len(group) > len(best):
                best = group
        return best

    @staticmethod
    def _peers(views, index):
        """Clickable views of the same class and id as `index`: its siblings,
        or its cousins when each sits in a wrapper of its own (PRO VIDEO's
        Mic sources: one `pro_item_button` per `pro_item`)."""
        mine = views[index]
        parent = mine.get("parent")
        if parent is None:
            return [mine]

        def same(j):
            v = views[j]
            return (v.get("clickable") and v.get("class") == mine.get("class")
                    and v.get("resource_id") == mine.get("resource_id"))
        sibs = [views[j] for j in views[parent].get("children", []) if same(j)]
        if len(sibs) >= 2:
            return sibs
        grand = views[parent].get("parent")
        if grand is None:
            return sibs
        out = []
        for uncle in views[grand].get("children", []):
            if views[uncle].get("resource_id") != views[parent].get("resource_id"):
                continue
            out += [views[j] for j in views[uncle].get("children", []) if same(j)]
        return out or sibs

    def _fingerprint(self, element):
        """Identity of an element: what it is, AND which row it belongs to.

        Label, kind and selector alone are not enough on a settings list: all
        twenty-odd inline switches are `switch_widget` with one shared id. As
        one identity, the first was handled and every later switch was taken
        as already done -- Video stabilization, Location tags, Shutter sound
        and the rest were written with no switch at all.

        And which LIST it sits in: the camera's top bar and its Quick
        controls panel both hold a `Flash` chip with one shared id. As one
        identity the panel's chip was lost, and with it the panel's order.
        """
        return (element.annotated(), element.kind, element.selector_kind,
                element.selector_value, getattr(element, "context", ""),
                getattr(element, "group", ""))

    def _elements(self, views):
        """Every element, each carrying its role and switch identity.

        Assigned here, from the dump the element was seen in, because that is
        the only moment the structure is certain. Classifying at press time
        failed for anything scrolled out of view: `Prioritize quality` -- a
        summary -- was looked for, not found, and reported as unlocatable
        rather than recognised as something never to press.
        """
        out = []
        for e in super()._elements(views):
            view = views[e.view_index] if 0 <= e.view_index < len(views) else {}
            # A clickable container with no text of its own, whose children
            # carry the labels, is named BY those children -- main_switch_
            # layout, spinner_layout, custom_text_layout. Listing it adds a
            # row nobody would write; the press still happens, through the
            # child label (elements._pressable_via).
            if not self._label_of(view) and any(
                    self._label_of(views[j])
                    for j in self._descendants(views, e.view_index)):
                continue
            # And the converse: an unlabelled control inside a container that
            # carries the name -- Face's items are `beauty_list_item_button`
            # inside a layout described "Smoothness". The container names it.
            if not self._label_of(view):
                up, named = view.get("parent"), False
                for _ in range(2):
                    if up is None or not 0 <= up < len(views):
                        break
                    if self._label_of(views[up]):
                        named = True
                        break
                    up = views[up].get("parent")
                if named:
                    continue
            out.append(e)
        for e in out:
            role, row, check = self._row_role(e, views)
            e.role = role
            e.group = self._list_of(views, e.view_index)
            if row is not None and 0 <= row < len(views):
                titles = [self._label_of(views[j])
                          for j in [row] + self._descendants(views, row)
                          if self._label_of(views[j])]
                if titles and titles[0] != e.label:
                    e.context = titles[0]
            if check is not None and 0 <= check < len(views):
                cv = views[check]
                e.switch_key = f"{cv.get('resource_id')}@{cv.get('bounds')}"
        return out

    # -- roles (docs/HANDLING.md) ----------------------------------------
    @staticmethod
    def _descendants(views, index):
        out, stack = [], list(views[index].get("children", []))
        while stack:
            j = stack.pop(0)
            if 0 <= j < len(views):
                out.append(j)
                stack[0:0] = views[j].get("children", [])
        return out

    @staticmethod
    def _label_of(view):
        return (view.get("text") or view.get("content_description") or "").strip()

    def _row_role(self, target, views):
        """(role, row index, checkable index) for a control, from structure.

        Read from the dump alone -- class, clickable, checkable, and which
        clickable row a label sits in -- so it holds for any app built from
        standard Android rows:

          switch        a Switch that is its own tap target (has `clickable`)
          inline        a Switch that is NOT clickable: its ROW operates it
          switch_row    the title of a row holding such an inline Switch
          option        a radio-style checkable (RadioButton, CheckedTextView)
          checkbox      a CheckBox
          summary       a second label inside the same clickable row
          title         the first label of a clickable row, or a control
          plain         anything with no clickable row around it

        Measured on the camera's Settings: `Shot suggestions` holds a
        non-clickable Switch, so pressing the row flips it; `Watermark` holds
        a clickable Switch behind a divider, so the row opens a screen and
        the switch is a separate target. They looked identical to a rule
        written in advance and are told apart here by one attribute.
        """
        i = target.view_index
        if not (0 <= i < len(views)):
            return "plain", None, None
        view = views[i]
        cls = (view.get("class") or "").split(".")[-1].lower()
        row = i if view.get("clickable") else _pressable_via(views, i)
        if view.get("checkable") or target.kind in ("toggle", "radio"):
            # Class first: Element.kind calls every checkable a "toggle", and
            # a radio button is not a switch -- it is one of a set of options.
            if "radio" in cls:
                return "option", row, i
            if "switch" in cls or "toggle" in cls or                     (target.kind == "toggle" and "checkbox" not in cls and "checked" not in cls):
                if view.get("clickable"):
                    return "switch", i, i
                return ("inline", row, i) if row is not None else ("switch", i, i)
            if "checkbox" in cls:
                return "checkbox", row, i
            return "option", row, i
        # A segmented choice -- two or more sibling, clickable, LEAF views of
        # one class under a single parent, each with its own description
        # (Watermark's Alignment Left / Center / Right). Which one is chosen
        # lives only in the accessibility text, so there is no attribute to
        # read, but pressing one changes the setting: it is an option.
        # Leaves only: quick-setting chips and zoom lens buttons hold child
        # views and must still be pressed.
        parent = view.get("parent")
        if view.get("clickable") and not view.get("children") and parent is not None:
            sibs = [j for j in views[parent].get("children", [])
                    if (views[j].get("class") == view.get("class")
                        and views[j].get("clickable") and not views[j].get("children"))]
            labels = {self._label_of(views[j]) for j in sibs}
            # Only when the row reads as a choice: named by descriptions
            # alone (Alignment Left / Center / Right are icons), or exactly
            # one of them marked selected (Natural / Warm). A pop-up's
            # Cancel / Turn on are text buttons with nothing selected --
            # taken for options, the first-launch dialog was never answered.
            by_desc = all(not (views[j].get("text") or "").strip() for j in sibs)
            one_selected = sum(1 for j in sibs if views[j].get("selected")) == 1
            if (len(sibs) >= 2 and len(labels) == len(sibs) and all(labels)
                    and (by_desc or one_selected)):
                return "option", row, None
        # A selection group: clickable siblings of one class and id, exactly
        # one of them `selected` -- Flash Off / Auto / On, the timers, the
        # ratios, the zoom lenses. Pressing one CHANGES the setting (an
        # earlier walk left the timer on 2 s and the zoom on 3x), so each is
        # an option: listed, never pressed.
        own = i if view.get("clickable") else row
        if own is not None and views[own].get("parent") is not None:
            sibs = self._peers(views, own)
            if len(sibs) >= 2 and sum(1 for s in sibs if s.get("selected")) == 1:
                return "option", row, None
        if row is None:
            return "plain", None, None
        inside = self._descendants(views, row)
        labelled = [j for j in inside if self._label_of(views[j])]
        if i != row and labelled and i != labelled[0]:
            # A switch row's state readout -- the "Off" beside the switch,
            # the same text the switch itself shows -- is the switch saying
            # its state, already written as the Off / On rows.
            switch_text = {self._label_of(views[j]).lower() for j in inside
                           if views[j].get("checkable")}
            if self._label_of(view).lower() in switch_text:
                return "state", row, None
            return "summary", row, None
        # A row holding a checkable that is not itself clickable is OPERATED
        # by pressing the row, so the row takes that control's role. Only
        # switches were checked for at first, and a radio row's title was
        # taken for a menu: pressing `Prioritize speed` on the Photo
        # enhancer screen SELECTED it, and Auto FPS was switched Off the same
        # way -- settings the walk had agreed never to change.
        inline = next((j for j in inside
                       if views[j].get("checkable") and not views[j].get("clickable")),
                      None)
        if inline is not None:
            kind = (views[inline].get("class") or "").lower()
            if "switch" in kind or "toggle" in kind:
                return "switch_row", row, inline
            if "checkbox" in kind:
                return "checkbox", row, inline
            return "option", row, inline
        return "title", row, None

    def _synthetic(self, label):
        return Element(label=label, kind="option", interactive=False, view_index=-1)

    def _operate_switch(self, target, views, tap_index, switch_index, depth,
                        path, path_selectors, node, ancestors):
        """The agreed switch policy: if OFF, turn ON, walk what it governs, restore.

        Returns the fingerprints of the controls this switch governs, so the
        caller does not list them a second time as ordinary siblings.

        What a switch governs is read from the `enabled` attribute, not from
        what newly appears. Measured on the Watermark screen: Info to show,
        Model name, Edit, Date, Time, Font and Alignment are on screen the
        whole time, `enabled=false` while the switch is off and
        `enabled=true` once it is on. Looking for NEW labels found only the
        switch bar relabelling itself Off -> On, and the real options were
        listed as siblings instead of under `On` where the sheet has them.

        Three outcomes, each handled and each restored:
          * governs controls  -> `Off` and `On` rows, governed walked under On
          * raises a dialog   -> `On`, the dialog listed under it, declined
          * navigates away    -> it is a shortcut, not a reveal: a leaf, as
                                 the sheet lists `Watermark (On/Off)` on the
                                 Settings list. Measured: pressing that switch
                                 opened the Watermark screen, which was then
                                 documented a second time.
        """
        sw = views[switch_index]
        was_on = bool(sw.get("checked"))
        sw_id = sw.get("resource_id")
        if was_on:
            return self._operate_switch_on(target, views, tap_index, switch_index,
                                           depth, path, path_selectors, node,
                                           ancestors, sw_id)
        own_row = set(self._descendants(views, tap_index)) | {tap_index}
        before = {}
        for e in self._elements(views):
            if e.view_index not in own_row:
                before[self._fingerprint(e)] = bool(views[e.view_index].get("enabled", True))
        centre = self._tap_point(views, tap_index)
        if centre is None:
            return set()
        self.driver.tap(*centre, settle=False)
        self._clicks += 1
        _, after_views, after_pkg = self._after_press(
            label_set(self._elements(views)), timeout=1.5)
        after_views = after_views or []
        after = self._elements(after_views)
        setting = path + [target.label]
        selectors = list(path_selectors) + [
            (target.selector_kind, target.selector_value)
            if target.selector_kind else ("text", target.label)]
        governed = set()

        if (after_pkg and after_pkg != self.package) or looks_like_dialog(after_views, self.package):
            self._emit(self._synthetic("On"), depth + 1, setting, selectors,
                       note="switch turned on")
            for e in after:
                self._emit(e, depth + 2, setting + ["On"], selectors,
                           note="dialog raised by turning the switch on")
            self._clear_blocking_dialog()
            self._switch_dialogs += 1
        elif not self._is_node(node.elements, after, node.own):
            # It took us somewhere else: a shortcut, not a reveal.
            self._switches_navigating += 1
            self._return_to(node)
        else:
            for e in after:
                fp = self._fingerprint(e)
                if fp in before and not before[fp] and \
                        after_views[e.view_index].get("enabled", True):
                    governed.add(fp)
            if governed:
                governed |= self._governed_below_fold(after_views, node, now_on=True)
                governed |= self._headers_of(node, governed)
                # At the switch's OWN level, as the To_Be sheet writes the
                # Watermark screen: J51 Watermark, J52 Off, J53 On, and the
                # governed options one level down under On (K54 Info to
                # show ...). Placing Off/On beneath the switch row put that
                # whole block one level too deep.
                self._emit(self._synthetic("Off"), depth, path, path_selectors,
                           note="switch state")
                self._emit(self._synthetic("On"), depth, path, path_selectors,
                           note="switch state -- governs the rows below")
                self._switches_revealing += 1
                self._switch_steps[tuple(path + ["On"])] = (target, sw_id, True)
                self._visit(depth + 1, path + ["On"], path_selectors, ancestors,
                            skip_tabs=True, entering=None, parent=node,
                            only=governed)

        # Restore from the switch's own screen. Walking what it governs can
        # leave the walk a screen or two away, and the restore once ran from
        # there -- found no switch, and left Watermark switched on.
        _, now, _ = self._await_stable()
        if not self._is_node(node.elements, self._elements(now or []), node.own):
            self._return_to(node)
        self._restore_switch(sw_id, was_on, switch_index, tap_index, target)
        return governed

    def _operate_switch_on(self, target, views, tap_index, switch_index, depth,
                           path, path_selectors, node, ancestors, sw_id):
        """A switch that is already ON: what does it govern?

        Its governed rows are visible and enabled, so nothing distinguishes
        them from ordinary siblings. Turning it OFF does: they go disabled.
        So turn it off, note what went disabled, turn it back ON at once, and
        write `Off` / `On` with the governed rows under `On` -- as the sheet
        writes Log (K98 Off, K99 On). The setting ends as it started.
        """
        self._switches_already_on += 1
        own_row = set(self._descendants(views, tap_index)) | {tap_index}
        before = {}
        for e in self._elements(views):
            if e.view_index not in own_row:
                before[self._fingerprint(e)] = bool(views[e.view_index].get("enabled", True))
        centre = self._tap_point(views, tap_index)
        if centre is None:
            return set()
        self.driver.tap(*centre, settle=False)
        self._clicks += 1
        _, off_views, off_pkg = self._after_press(label_set(self._elements(views)), 1.5)
        off_views = off_views or []
        governed = set()
        if not (off_pkg and off_pkg != self.package) and \
                not looks_like_dialog(off_views, self.package):
            for e in self._elements(off_views):
                fp = self._fingerprint(e)
                if before.get(fp) and not off_views[e.view_index].get("enabled", True):
                    governed.add(fp)
            if governed:
                governed |= self._governed_below_fold(off_views, node, now_on=False)
                governed |= self._headers_of(node, governed)
        else:
            self._clear_blocking_dialog()
        self._restore_switch(sw_id, True, switch_index, tap_index, target)
        if governed:
            self._emit(self._synthetic("Off"), depth, path, path_selectors,
                       note="switch state")
            self._emit(self._synthetic("On"), depth, path, path_selectors,
                       note="switch state -- governs the rows below")
            self._switches_revealing += 1
            self._switch_steps[tuple(path + ["On"])] = (target, sw_id, True)
            self._visit(depth + 1, path + ["On"], path_selectors, ancestors,
                        skip_tabs=True, entering=None, parent=node, only=governed)
        return governed

    def _list_panel(self, fresh, target, depth, child_path, child_selectors,
                    node, elements, handled, before=frozenset(),
                    inherited=frozenset()):
        """A press that opened a panel on this screen: list it, close it.

        Agreed handling (docs/HANDLING.md): options are listed under the
        control and never selected (Flash, Timer, Ratio); a dial is listed
        by its title, its value and its Close (Exposure, Face) and never
        moved. The walk then leaves by the panel's own close control, and
        returns to this screen if the panel took it elsewhere -- otherwise
        the next chip in the row is hidden behind the panel.
        """
        # Read the panel once more when it has settled: a dial's Close
        # slides in after its title and value, and was missing.
        time.sleep(0.8)
        _, late_views, _ = self._await_stable()
        known = {self._fingerprint(e) for e in fresh} | before | inherited
        fresh = list(fresh) + [e for e in self._elements(late_views or [])
                               if self._fingerprint(e) not in known]
        # Its close control may carry the same identity as one already on
        # the screen before -- a dial's `Close` and the panel's `Close` are
        # both just "Close" -- so it is looked for on the settled panel
        # itself, not among what is new.
        # Text of the screen above coming back into view is not the panel's:
        # Face's dial was listed with the camera's "Ready" status beside it.
        above = {f[0] for f in inherited}
        fresh = [e for e in fresh if e.interactive or e.label not in above]
        new_ones = list(fresh)
        # Closed only by a back control that came WITH the panel. Any other
        # one on screen belongs to the screen: in the MORE modes it is the
        # mode's own Back, which leaves for PHOTO, and the walk then fell
        # out of the camera. Without one, the return ladder takes over.
        closer = next((e for e in fresh if self._is_back(e)), None)
        if closer is None and late_views:
            # A dial's Close carries the same identity as the panel's own
            # Close, so "came with it" is judged by what was on screen.
            closer = next((e for e in self._elements(late_views)
                           if self._is_back(e)
                           and self._fingerprint(e) not in before
                           and self._fingerprint(e) not in inherited), None)
            if closer is not None:
                fresh = list(fresh) + [closer]
        selector = self._selector_in(late_views or [])
        if selector is not None and any(e.group == selector[1] for e in fresh):
            # A carousel that selects: walked item by item, the selection put
            # back (agreed). Its announcement and the selected item's title
            # are not rows of their own.
            for e in new_ones:
                handled.add(self._fingerprint(e))
            self._walk_selector(selector, target, depth + 1, child_path,
                                child_selectors, node, inherited)
            self._panels_listed += 1
            _, views, _ = self._await_stable()
            if not self._is_node(elements, self._elements(views or []), node.own):
                self._return_to(node)
            return
        toggles = {}
        for e in fresh:
            state = self._state_of(e, late_views or [])
            if state is not None:
                toggles[self._fingerprint(e)] = state
        readouts = {state for _, state in toggles.values()}
        for e in self._dedupe_controls(fresh):
            if self._fingerprint(e) in toggles:
                self._operate_state_toggle(e, toggles[self._fingerprint(e)],
                                           depth + 1, child_path, child_selectors)
                continue
            if e.label in readouts:
                continue                    # the toggle's own state text
            if self._is_back(e):
                note = "back control"
            elif e.role == "option":
                note = "option -- listed, not selected"
                self._options_listed += 1
            elif e.interactive:
                note = self._worth_pressing(e) or ""
            else:
                note = ""               # a title or a value: just listed
            self._emit(e, depth + 1, child_path, child_selectors, note=note)
        # Only what was new is done with. The dial's `Close` shares its
        # identity with the panel's own `Close`, which still needs its row.
        for e in new_ones:
            handled.add(self._fingerprint(e))
        self._panels_listed += 1
        if closer is not None:
            _, views, _ = self._await_stable()
            found = self._find_element(closer.label, views or [])
            if found is not None and self._click(found, views):
                self._after_press(label_set(self._elements(views)))
        # Then back to this screen the usual way -- by the control that
        # opened it (Quick controls), else its back control. Never a bare
        # hardware Back: on the camera's own screen that closes the camera.
        # Home means the control just pressed is in view again -- matched by
        # its full identity, list included, so the top bar's `Flash` cannot
        # stand in for the panel's. Merely sharing most of what is visible is
        # not enough: with the panel's options still open the chips are
        # hidden, and Timer, Ratio, Exposure and Face were not found.
        # And most of the screen's own controls with it: SLOW MOTION's Flash
        # options left Flash in view but hid Video size, Bar and HDR.
        own_before = (before & node.own) if node.own else set(before)

        def home() -> bool:
            _, views, _ = self._await_stable()
            current = self._elements(views or [])
            here = {self._fingerprint(e) for e in current}
            mine = self._fingerprint(target)
            enough = (not own_before
                      or len(own_before & here) >= 0.8 * len(own_before))
            return (mine in here and enough
                    and self._is_node(elements, current))
        if not home():
            # Options a chip opened are closed by pressing that chip again.
            # Otherwise PHOTO's Flash options stayed open after the walk
            # "returned" by the PHOTO tab, and the next press -- Filters --
            # merely closed them: Filters' 124 rows were never walked.
            _, views, _ = self._await_stable()
            again = next((e for e in self._elements(views or [])
                          if self._fingerprint(e) == self._fingerprint(target)), None)
            if again is not None and self._click(again, views):
                self._after_press(label_set(self._elements(views)), timeout=1.5)
        if not home():
            self._return_to(node)
            if not home():
                logger.info("after %r the screen did not come back as it was",
                            target.label)

    def _state_of(self, element, views):
        """(name, state) for a control that shows its own state, else None.

        A clickable control whose label ends with the text of one of its own
        child views: "Face retouching: Off" holding a child "Off". The child
        is the state readout; the label without it is the control's name.
        """
        if not element.interactive or not 0 <= element.view_index < len(views):
            return None
        label = element.label or ""
        for j in self._descendants(views, element.view_index):
            said = (views[j].get("text") or "").strip()
            if said and said != label and label.endswith(said):
                name = label[: -len(said)].rstrip(" :,-\u2013")
                if name:
                    return name, said
        return None

    def _operate_state_toggle(self, element, state, depth, path, selectors):
        """Agreed: a state toggle is handled as a switch -- `Off` / `On` rows,
        what turning it on shows listed under `On` -- and put back at once."""
        name, was = state
        rid = element.selector_value if element.selector_kind == "id" else None
        self._emit(Element(label=name, kind="text", interactive=True,
                           view_index=element.view_index), depth, path, selectors,
                   note="toggle -- turned on to read, put back")
        _, views, _ = self._await_stable()
        views = views or []
        mine = next((e for e in self._elements(views)
                     if self._fingerprint(e) == self._fingerprint(element)), None)
        if mine is None or not self._click(mine, views):
            return
        before = self._signature(self._elements(views))
        self._after_press(label_set(self._elements(views)), timeout=1.5)
        time.sleep(0.5)
        _, on_views, _ = self._await_stable()
        on_views = on_views or []
        flipped = next((e for e in self._elements(on_views)
                        if e.label.startswith(name) and e.label != element.label), None)
        shown = [e for e in self._elements(on_views)
                 if self._fingerprint(e) not in before
                 and not e.label.startswith(name)
                 and (flipped is None or e.label != (self._state_of(flipped, on_views) or ("", ""))[1])]
        if flipped is None:
            self._emit(self._synthetic(was), depth, path + [name], selectors,
                       note="state shown; pressing it changed nothing")
            return
        now = (self._state_of(flipped, on_views) or (name, ""))[1]
        self._emit(self._synthetic(was), depth, path, selectors, note="toggle state")
        self._emit(self._synthetic(now), depth, path, selectors,
                   note="toggle state -- shows the rows below")
        for e in self._dedupe(shown):
            self._emit(e, depth + 1, path + [now], selectors)
        # Put it back, and make sure it went.
        for _ in range(2):
            if self._click(flipped, on_views):
                self._after_press(label_set(self._elements(on_views)), timeout=1.5)
            _, back, _ = self._await_stable()
            back = back or []
            if any(e.label == element.label for e in self._elements(back)):
                self._switches_restored += 1
                return
            flipped = next((e for e in self._elements(back)
                            if e.label.startswith(name)), None)
            on_views = back
            if flipped is None:
                break
        logger.warning("could not put %r back to %r", name, was)
        self._switches_not_restored += 1

    def _relabelled(self, target, before_views, after_views):
        """A control that now shows another label in the same place, or None.

        The pressed one first -- FOOD's `FOOD_BLUR_EFFECT_ON` became `_OFF`
        -- then any other: Quick controls' stabilisation chip keeps its
        label, and it is the TOP BAR's copy that flips to
        `SUPER_VIDEO_STABILIZATION_ON`. Either way the press changed a
        setting. Same id, overlapping place (a new label can change the
        width), a label that was nowhere on the screen before.
        """
        if not after_views:
            return None
        before_labels = {e.label for e in self._elements(before_views)}
        olds = []
        for e in self._elements(before_views):
            v = before_views[e.view_index]
            if e.interactive and v.get("resource_id") and box_of(v):
                olds.append((e.view_index == target.view_index, v.get("resource_id"),
                             box_of(v), e.label))
        olds.sort(key=lambda o: not o[0])          # the pressed control first
        carousel = self._selector_in(after_views)
        for e in self._elements(after_views):
            if not e.interactive or e.label in before_labels:
                continue
            # Options are not a relabelled control: Resolution's choices take
            # the top bar's place, with the chips' own id, and were "put
            # back" until PHOTO lost its way. Nor is a carousel's item.
            if e.role == "option" or (carousel and e.group == carousel[1]):
                continue
            v = after_views[e.view_index]
            box = box_of(v)
            if not box:
                continue
            for _, rid, old_box, old_label in olds:
                if (v.get("resource_id") == rid and old_label != e.label
                        and self._overlap(old_box, box) >= 0.5):
                    e.was_label = old_label         # what it showed before
                    return e
        return None

    @staticmethod
    def _overlap(a, b) -> float:
        """Intersection over the smaller of two boxes."""
        w = min(a[2], b[2]) - max(a[0], b[0])
        h = min(a[3], b[3]) - max(a[1], b[1])
        if w <= 0 or h <= 0:
            return 0.0
        small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        return (w * h) / max(1, small)

    def _undo_relabel(self, target, flipped, depth, path, selectors):
        """Record the state the press switched to, then put it back -- by
        pressing what was pressed again, until the old label shows."""
        was = getattr(flipped, "was_label", "") or target.label
        self._emit(flipped, depth + 1, path, selectors,
                   note="state after pressing -- put back")
        for _ in range(2):
            _, views, _ = self._await_stable()
            views = views or []
            now = (next((e for e in self._elements(views)
                         if self._fingerprint(e) == self._fingerprint(target)), None)
                   or next((e for e in self._elements(views) if e.label == flipped.label), None))
            if now is None or not self._click(now, views):
                break
            self._after_press(label_set(self._elements(views)), timeout=1.5)
            _, views, _ = self._await_stable()
            if any(e.label == was for e in self._elements(views or [])):
                self._switches_restored += 1
                return
        logger.warning("could not put %r back", target.label)
        self._switches_not_restored += 1

    def _emit_note(self, element, depth, note):
        """Add a note to the row already written for `element`."""
        for row in reversed(self.rows):
            if row.depth == depth and row.raw_label == element.label:
                row.note = (row.note + "; " if row.note else "") + note
                return

    def _selector_in(self, views):
        """(index, group, announcement) of a carousel that selects, or None.

        A horizontally scrolling list that carries a description of its own
        and holds clickable items: the description announces what is
        selected ("Original Filter", "Classic film Filter"). The mode strip is
        the same kind of thing and is handled as tabs instead.
        """
        for i, v in enumerate(views):
            said = v.get("content_description")
            box = box_of(v)
            if not (v.get("scrollable") and said and box):
                continue
            if (box[2] - box[0]) <= (box[3] - box[1]):
                continue
            group = f"{v.get('class') or ''}/{v.get('resource_id') or ''}"
            items = [e for e in self._elements(views)
                     if e.group == group and e.interactive]
            if len(items) < 2 or any(e.label.strip().lower() in self._tab_labels
                                     for e in items):
                continue
            return i, group, said
        return None

    def _announced(self, group):
        _, views, _ = self._await_stable()
        for v in views or []:
            if f"{v.get('class') or ''}/{v.get('resource_id') or ''}" == group:
                return v.get("content_description") or ""
        return ""

    def _enumerate_scrolled(self, views, enabled=None):
        """The screen's sweep, plus any row of items cut off at its edge.

        PHOTO's Quick controls row holds nine chips and shows seven: Motion
        photo is cut in half at the right edge, and Filters and Face lie
        beyond it. The row is not marked scrollable, so the sweep never moved
        it and those two were never seen. A horizontal list whose last item
        is clipped by the list's edge has more to show; it is dragged along
        until nothing new appears, and dragged back.
        """
        found = super()._enumerate_scrolled(views, enabled)
        _, views, _ = self._await_stable()
        views = views or []
        for index, box in self._clipped_rows(views):
            group = f"{views[index].get('class') or ''}/{views[index].get('resource_id') or ''}"
            seen = {self._fingerprint(e) for e in found}
            y = (box[1] + box[3]) // 2
            span = box[2] - box[0]
            start, end = box[2] - max(10, span // 10), box[0] + max(10, span // 10)
            moves = 0
            for _ in range(self.max_scrolls):
                self.driver.swipe(start, y, end, y, self.scroll_duration_ms)
                self._scrolls += 1
                moves += 1
                time.sleep(self.scroll_settle)
                _, now, _ = self._await_stable()
                new = [e for e in self._elements(now or [])
                       if self._fingerprint(e) not in seen]
                if not new:
                    break
                # After the last item already known from this row.
                at = max((i for i, e in enumerate(found) if e.group == group),
                         default=len(found) - 1)
                for e in new:
                    seen.add(self._fingerprint(e))
                    at += 1
                    found.insert(at, e)
                    if enabled is not None and 0 <= e.view_index < len(now or []):
                        enabled[self._fingerprint(e)] = bool(
                            now[e.view_index].get("enabled", True))
            for _ in range(moves):
                self.driver.swipe(end, y, start, y, self.scroll_duration_ms)
                time.sleep(self.scroll_settle)
            self._clipped_rows_swept += 1
        return found

    def _find_element_scrolled(self, label, views):
        """As the base lookup, then along any cut-off row of items.

        Filters and Face sit past the edge of PHOTO's Quick controls row;
        the sweep sees them by dragging the row, and pressing one needs the
        same drag -- in both directions, as the row may be left either way.
        """
        found, views = super()._find_element_scrolled(label, views)
        if found is not None:
            return found, views
        for index, box in self._clipped_rows(views) or self._clipped_rows(
                self._await_stable()[1] or []):
            y = (box[1] + box[3]) // 2
            span = box[2] - box[0]
            right, left = box[2] - max(10, span // 10), box[0] + max(10, span // 10)
            for start, end in ((right, left), (left, right)):
                last = None
                for _ in range(self.max_scrolls):
                    self.driver.swipe(start, y, end, y, self.scroll_duration_ms)
                    self._scrolls += 1
                    time.sleep(self.scroll_settle)
                    _, views, _ = self._await_stable()
                    views = views or []
                    hit = self._find_element(label, views)
                    if hit is not None and self._tap_point(views, hit.view_index):
                        return hit, views
                    now = label_set(self._elements(views))
                    if now == last:
                        break
                    last = now
        return None, list(views)

    def _clipped_rows(self, views):
        """(index, box) of horizontal lists whose last item is cut off.

        Not marked scrollable, not a selector (no announcement), holding two
        or more clickable items, the last of which is markedly narrower than
        the others and touches the list's edge.
        """
        out = []
        for i, v in enumerate(views):
            box = box_of(v)
            cls = v.get("class") or ""
            if (not box or v.get("scrollable") or v.get("content_description")
                    or not cls.endswith(("RecyclerView", "ListView"))
                    or (box[2] - box[0]) <= (box[3] - box[1])):
                continue
            kids = [box_of(views[j]) for j in v.get("children", [])
                    if views[j].get("clickable") and box_of(views[j])]
            if len(kids) < 2:
                continue
            kids.sort(key=lambda b: b[0])
            widths = sorted(b[2] - b[0] for b in kids)
            usual = widths[len(widths) // 2]
            last = kids[-1]
            if last[2] >= box[2] - 2 and (last[2] - last[0]) < 0.8 * usual:
                out.append((i, box))
        return out

    def _items_of(self, group):
        """The carousel's visible items, left to right, and the screen."""
        _, views, _ = self._await_stable()
        views = views or []
        items = [e for e in self._elements(views) if e.group == group and e.interactive]
        items.sort(key=lambda e: (box_of(views[e.view_index]) or (0, 0, 0, 0))[0])
        return items, views

    def _walk_selector(self, selector, opener, depth, path, selectors, node,
                       inherited):
        """Walk a carousel whose items are selections (agreed for Filters).

        Each item is selected in turn -- there is no other way to see them
        all, or to see an item's own settings. Selected, it is pressed once
        more to open its editor, whose contents are listed (sliders never
        moved, Reset never pressed) and which is left by its Close. An item
        that does not select (Custom filters) is handled like any other
        press. At the end the item selected to begin with is selected again.
        """
        _, group, first = selector
        self._selectors_walked += 1
        done, selecting = set(), []
        above = {f[0] for f in inherited}

        def reopen():
            # Back on the carousel, if a press took the walk off it.
            if self._selector_in(self._await_stable()[1] or []) is not None:
                return True
            self._return_to(node)
            _, views, _ = self._await_stable()
            again = self._find_element(opener.label, views or [])
            if again is not None and self._click(again, views):
                self._after_press(label_set(self._elements(views)))
            return self._selector_in(self._await_stable()[1] or []) is not None

        # The carousel as it opened: what a selection brings ("View original")
        # is judged against this, not against the previous item -- otherwise
        # only the first filter selected was shown to bring it.
        base = self._signature(self._elements(self._await_stable()[1] or []))
        stalls = 0
        while self._budget_left() and stalls < 2:
            items, views = self._items_of(group)
            todo = [e for e in items if e.label not in done]
            if not todo:
                # Move along: re-select the right-most item that selects; the
                # carousel centres it and shows what lies beyond.
                # Never the item already selected: pressing that opens its
                # editor instead of moving the carousel.
                said = self._announced(group)
                ahead = [e for e in items if e.label in selecting
                         and e.label not in said]
                if not ahead or not self._click(ahead[-1], views):
                    break
                self._after_press(label_set(self._elements(views)))
                stalls += 1
                continue
            stalls = 0
            item = todo[0]
            done.add(item.label)
            why = self._worth_pressing(item)
            if why is not None:
                self._emit(item, depth, path, selectors, note=why)
                continue
            self._emit(item, depth, path, selectors)
            before_said = self._announced(group)
            standing = self._elements(views)
            before = self._signature(standing)
            if not self._click(item, views):
                continue
            _, after_views, after_pkg = self._after_press(label_set(standing))
            if after_pkg and after_pkg != self.package:
                self._foreign_skipped += 1
                reopen()
                continue
            if self._announced(group) == before_said:
                if item.label in before_said:
                    # The item already selected: pressing it again selected
                    # nothing new. It is the one to put back at the end.
                    selecting.append(item.label)
                # Not a selection: a panel of its own (Custom filters' picker).
                time.sleep(0.6)
                _, after_views, _ = self._await_stable()
                fresh = [e for e in self._elements(after_views or [])
                         if self._fingerprint(e) not in before
                         and self._fingerprint(e) not in inherited]
                for e in self._dedupe(fresh):
                    if self._is_back(e):
                        note = "back control"
                    elif e.role == "option":
                        note = "option -- listed, not selected"
                    elif e.interactive:
                        note = self._worth_pressing(e) or ""
                    else:
                        note = ""
                    self._emit(e, depth + 1, path + [item.label], selectors, note=note)
                if self._selector_in(after_views or []) is None or fresh:
                    self.driver.press_back(settle=False)
                    self._after_press(label_set(self._elements(after_views or [])))
                reopen()
                continue
            selecting.append(item.label)
            # Selected. What selecting brought (View original ...), then the
            # item's own editor, opened by pressing it again.
            time.sleep(0.4)
            _, sel_views, _ = self._await_stable()
            said_now = self._announced(group)
            brought = [e for e in self._elements(sel_views or [])
                       if self._fingerprint(e) not in base and e.group != group
                       and e.interactive and e.label not in above]
            again = next((e for e in self._items_of(group)[0] if e.label == item.label), None)
            editor = []
            if again is not None:
                _, sel_views, _ = self._await_stable()
                standing = self._elements(sel_views or [])
                ed_before = self._signature(standing)
                if self._click(again, sel_views):
                    self._after_press(label_set(standing))
                    time.sleep(0.6)
                    _, ed_views, _ = self._await_stable()
                    # Not the item's own name again ("Monotone [Title]"),
                    # nor the carousel announcing it (agreed shape).
                    editor = [e for e in self._elements(ed_views or [])
                              if self._fingerprint(e) not in ed_before
                              and e.label not in (said_now, item.label)
                              and e.label not in above]
            closer = None
            for e in self._dedupe(editor):
                if self._is_back(e):
                    note = "back control"
                    closer = closer or e
                elif e.interactive:
                    note = self._worth_pressing(e) or "setting -- listed, not changed"
                else:
                    note = ""
                self._emit(e, depth + 1, path + [item.label], selectors, note=note)
            for e in self._dedupe(brought):
                self._emit(e, depth + 1, path + [item.label], selectors,
                           note=self._worth_pressing(e) or "")
            if closer is not None:
                _, views, _ = self._await_stable()
                found = self._find_element(closer.label, views or [])
                if found is not None and self._click(found, views):
                    self._after_press(label_set(self._elements(views)))
            if self._selector_in(self._await_stable()[1] or []) is None:
                reopen()

        self._restore_selection(group, first, selecting, done)

    def _restore_selection(self, group, first, selecting, seen_items=()):
        """Select again what the carousel announced before the walk."""
        if self._announced(group) == first:
            self._selections_restored += 1
            return
        wanted = max((label for label in selecting if label in first),
                     key=len, default=None)
        if wanted is None:
            # The item named may never have registered as "selecting" (the
            # front camera's Portrait effects): any item seen will do.
            wanted = max((label for label in seen_items if label in first),
                         key=len, default=None)
            if wanted is not None:
                selecting = list(selecting) + [wanted]
        if wanted is None:
            logger.warning("cannot tell which item %r names", first)
            return
        for _ in range(self.max_scrolls):
            items, views = self._items_of(group)
            hit = next((e for e in items if e.label == wanted), None)
            # Not in view: select the left-most item that selects, which
            # brings the carousel back towards the start -- never the one
            # already selected, which would open its editor instead.
            said = self._announced(group)
            step = hit or next((e for e in items if e.label in selecting
                                and e.label not in said), None)
            if step is None or not self._click(step, views):
                break
            self._after_press(label_set(self._elements(views)))
            if hit is not None:
                break
        if self._announced(group) == first:
            self._selections_restored += 1
        else:
            logger.warning("could not put the selection back to %r", first)

    def _list_captions(self, appeared, target, depth, path, path_selectors,
                       handled):
        """A press that only showed a caption: write it, then press again.

        Agreed handling: the caption is a `[Title]` row directly after the
        control, at its depth (the sheet's `Not available with blur effect.
        [Title]` after Lighting). Nothing in the layout tells an information
        caption from a toggle -- Night shot shows "Night shot Off" and HAS
        switched night shot off -- so the control is pressed once more, which
        puts a toggle back and costs a caption control nothing.
        """
        for e in self._dedupe(appeared):
            caption = Element(label=e.label, kind="title", interactive=False,
                              view_index=e.view_index)
            self._emit(caption, depth, path, path_selectors,
                       note=f"caption shown on pressing {target.label}")
            handled.add(self._fingerprint(e))
        self._captions_listed += 1
        _, views, _ = self._await_stable()
        views = views or []
        again = self._find_element(target.label, views)
        if again is not None and self._click(again, views):
            self._presses_undone += 1
        # Let the caption go before the next press, or it is read again as
        # something THAT press showed.
        shown = {e.label for e in appeared}
        deadline = time.time() + 4.0
        while time.time() < deadline:
            time.sleep(0.4)
            _, views, _ = self._await_stable()
            if not shown & {e.label for e in self._elements(views or [])}:
                break

    def _governed_below_fold(self, views, node, now_on):
        """Governed rows the visible check could not see.

        Watermark governs Info to show, Model name, Date, Time, Style, Font
        and Alignment; only the first three are on screen with the switch.
        Comparing visible rows alone put those three under `On` and left the
        rest as ordinary siblings, unopened, because they were disabled when
        the screen was swept. Sweep the whole screen in the new state and
        compare with the node's own sweep. Only for a switch that already
        showed it governs something, on a screen that scrolls.
        """
        if not node.enabled or scrollable_container(views) is None:
            return set()
        now = {}
        self._enumerate_scrolled(views, now)
        return {fp for fp, on in now.items()
                if fp in node.enabled and node.enabled[fp] != now_on and on == now_on}

    @staticmethod
    def _list_of(views, index) -> str:
        """Class and id of the nearest ancestor that is a list or scrolls."""
        i = views[index].get("parent") if 0 <= index < len(views) else None
        while i is not None and 0 <= i < len(views):
            v = views[i]
            cls = v.get("class") or ""
            if v.get("scrollable") or cls.endswith(("RecyclerView", "ListView")):
                return f"{cls}/{v.get('resource_id') or ''}"
            i = v.get("parent")
        return ""

    def _headers_of(self, node, governed):
        """Section headers that belong to what a switch governs.

        `Info to show` and `Style` head Watermark's options but are plain
        text: they never change `enabled`, so the switch test leaves them
        behind as siblings of the switch, where the sheet has them under `On`
        (K54 Info to show). A plain label lying among the governed rows, or
        directly above the first of them, heads them -- provided it sits IN
        THE SAME LIST as them. Read from order and layout, never the words.

        A switch's own description stays beside Off / On (agreed with the
        sheet's author): Log's "Add an option to record in Log format..." is
        directly above its two mode options too, but in a description block
        of its own, outside the list the options are in.
        """
        order = [self._fingerprint(e) for e in node.elements]
        at = [i for i, fp in enumerate(order) if fp in governed]
        if not at:
            return set()
        lists = {node.elements[i].group for i in at if node.elements[i].group}
        out = set()
        for i in range(max(0, at[0] - 1), at[-1]):
            e = node.elements[i]
            if (order[i] not in governed and not e.interactive
                    and e.role == "plain" and e.group in lists):
                out.add(order[i])
        return out

    def _switch_beside(self, views, target, sw_id):
        """The switch that belongs to `target`'s row, on the screen as it is now.

        Restoring by view index assumed the list had not moved. After a sweep
        it has, and a Settings list holds a dozen switches sharing one id, so
        the nearest index can be another setting's switch.
        """
        fp = self._fingerprint(target)
        mine = next((e for e in self._elements(views) if self._fingerprint(e) == fp), None)
        if mine is None:
            return None
        _, row, _ = self._row_role(mine, views)
        if row is not None:
            inside = [j for j in [row] + list(self._descendants(views, row))
                      if views[j].get("checkable")]
            if len(inside) == 1:
                return inside[0]
        box = box_of(views[mine.view_index])
        if box is None:
            return None
        y = (box[1] + box[3]) / 2
        match = [j for j, v in enumerate(views)
                 if v.get("checkable") and (not sw_id or v.get("resource_id") == sw_id)
                 and box_of(v) is not None]
        if not match:
            return None
        return min(match, key=lambda j: abs((box_of(views[j])[1] + box_of(views[j])[3]) / 2 - y))

    def _locate_switch(self, views, target, sw_id, switch_index):
        """Index of `target`'s switch on the screen as it is now, or None.

        In order: on screen by the target's row; the one switch on screen
        with this id; scrolled to by the target's label; nearest by index.
        The label alone is not enough -- a switch bar's label is its own state
        ("Off" becomes "On" once flipped), so it cannot be found by the text
        it had before.
        """
        if target is not None and self._find_element(target.label, views) is not None:
            j = self._switch_beside(views, target, sw_id)
            if j is not None and not self._under_chrome(views, j):
                return j, views
        same = [k for k, v in enumerate(views)
                if v.get("checkable") and sw_id and v.get("resource_id") == sw_id]
        if len(same) == 1 and not self._under_chrome(views, same[0]):
            return same[0], views
        if target is not None:
            # Scroll until the switch ITSELF is in view. Its label is not
            # enough: Watermark's switch bar scrolls away while the toolbar
            # title "Watermark" stays on screen, so a search by label found
            # the title, never scrolled, and the restore gave up with the
            # setting left on.
            span = self._swipe_span(views)
            if span is not None:
                for down in (False, True):
                    for _ in range(self.max_scrolls):
                        j = self._switch_beside(views, target, sw_id)
                        if j is not None and not self._under_chrome(views, j):
                            return j, views
                        moved = self._scroll_once(span, down=down)
                        if not moved or label_set(self._elements(moved)) ==                                 label_set(self._elements(views)):
                            views = moved or views
                            break
                        views = moved
        match = [k for k, v in enumerate(views)
                 if v.get("checkable") and (not sw_id or v.get("resource_id") == sw_id)]
        if not match or switch_index is None:
            return None, views
        return min(match, key=lambda k: abs(k - switch_index)), views

    def _under_chrome(self, views, j) -> bool:
        """Too much of view `j` is under the system bars to tap it.

        A sticky switch bar pins itself to the top of a scrolled list, half
        under the status bar (Watermark's at [30,0][1050,158], the bar at
        [0,0][1080,96]); tapping its centre hit the status bar and the
        restore failed three runs running. See _tap_point.
        """
        return self._tap_point(views, j) is None

    def _flip_to(self, want_on, target, sw_id, switch_index=None) -> bool:
        """Set a switch to `want_on`, and confirm it took.

        One tap per attempt, then wait for the switch to REPORT the new
        state. Reading straight after the tap saw the old state, tapped again,
        and turned Watermark back on -- "could not restore a switch to off".
        """
        for _ in range(2):
            _, views, _ = self._await_stable()
            j, views = self._locate_switch(views or [], target, sw_id, switch_index)
            if j is None:
                return False
            if bool(views[j].get("checked")) == want_on:
                return True
            tap = j if views[j].get("clickable") else (_pressable_via(views, j) or j)
            centre = self._tap_point(views, tap)
            if centre is None:
                return False
            self.driver.tap(*centre, settle=False)
            self._clicks += 1
            deadline = time.time() + 2.5
            while time.time() < deadline:
                time.sleep(0.3)
                _, now, _ = self._await_stable()
                k, now = self._locate_switch(now or [], target, sw_id, switch_index)
                if k is not None and bool(now[k].get("checked")) == want_on:
                    return True
        return False

    def _set_switch(self, target, sw_id, want_on) -> bool:
        """Put `target`'s switch in the wanted state (used by replay)."""
        return self._flip_to(want_on, target, sw_id)

    def _restore_switch(self, sw_id, was_on, switch_index, tap_index, target=None):
        if self._flip_to(was_on, target, sw_id, switch_index):
            self._switches_restored += 1
            return
        logger.warning("could not restore a switch to %s", "on" if was_on else "off")
        self._switches_not_restored += 1

    def _swipe_span(self, views):
        """Refuse to swipe a navigation strip.

        A horizontal container is usually a carousel of content -- the filter
        list, which hides eight of its twelve entries off-screen. It is
        sometimes a row of TABS, and swiping that switches mode.

        Detecting the damage afterwards is too late, and was tried: the walk
        counted 108 swipes that changed the screen, but each had already
        moved PHOTO to VIDEO before it was noticed, poisoning the node it was
        enumerating. Benchmark 35 of 55 down to 16.

        So the strip is excluded before the swipe. Anything holding a control
        that is already a tab is navigation, and navigation is walked by
        pressing it, never by dragging it.
        """
        span = super()._swipe_span(views)
        if span is not None and span[0] == "h":
            container = scrollable_container(views)
            if container is not None and container.get("content_description"):
                # A horizontal list that announces a selection is a selector:
                # swiping the filter carousel SELECTED filter after filter.
                # It is walked by _walk_selector, never swept.
                self._selectors_not_swiped += 1
                return None
        if span is None or span[0] != "h" or not self._tab_labels:
            return span
        container = scrollable_container(views)
        box = box_of(container) if container is not None else None
        if not box:
            return span
        for element in self._elements(views):
            if element.label.strip().lower() not in self._tab_labels:
                continue
            if not (0 <= element.view_index < len(views)):
                continue
            inner = box_of(views[element.view_index])
            if inner and (box[0] - 2 <= inner[0] and inner[2] <= box[2] + 2
                          and box[1] - 2 <= inner[1] and inner[3] <= box[3] + 2):
                self._strips_not_swiped += 1
                return None
        return span

    def _dedupe_controls(self, elements: Sequence[Element]) -> List[Element]:
        """One CONTROL per label; every displayed value kept.

        Face's items each show a value, all "0": deduplicated by label, only
        Smoothness kept its value. Controls are what the mode strip renders
        twice; values are not."""
        controls = {e.label.strip().lower() for e in elements if e.interactive}
        seen, out = set(), []
        for element in elements:
            key = element.label.strip().lower()
            if not key:
                continue
            if element.interactive:
                if key in seen:
                    continue
                seen.add(key)
            elif key in controls:
                continue        # a control's own caption (Mic's "OMNI" under OMNI)
            out.append(element)
        return out

    def _dedupe(self, elements: Sequence[Element]) -> List[Element]:
        """One control per label. The mode strip is rendered twice."""
        seen, out = set(), []
        for element in elements:
            key = element.label.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(element)
        return out

    def _already_documented(self, signature: Set[tuple],
                            context: str) -> Optional[str]:
        """Where this screen was listed before, if it was.

        A common menu is documented ONCE. The deliverable proves that every
        menu and element is reachable; listing the same quick settings again
        under every mode does not add proof, it adds rows.

        The threshold is high -- 0.9, not the 0.55 used for "did navigation
        land where I meant". Photo and Video are both viewfinders and share
        most of their chrome; at 0.55 Video would be skipped as already
        covered, losing a genuine branch.

        `context` is the branch the screen was reached through, and screens
        are only ever compared within the same one. A panel that carries the
        same NAME under two modes is not the same panel: Quick Control offers
        Motion photo in Photo, and Super steady, Video size, Dual recording
        and Auto framing in Video. They overlap far past 0.9 on the features
        they share, so comparing across branches merged all three into
        whichever mode happened to be walked first.

        Measured, before this: PORTRAIT was walked first and took 108 rows
        under its Quick Control -- including Motion photo, which belongs to
        Photo -- while PHOTO ended with **zero**, and Auto framing was never
        recorded at all.

        "Covered once" means once per place it genuinely exists, not once per
        name.
        """
        for (branch, where), seen in self._documented.items():
            if branch != context:
                continue
            overlap = len(signature & seen) / max(1, len(signature | seen))
            if overlap >= self.documented_similarity:
                return where
        return None

    # -- the walk --------------------------------------------------------
    def _visit(self, depth, path, path_selectors, ancestors, skip_tabs=False,
               entering=None, parent=None, only=None):
        """List this screen, then walk each item that opens something."""
        if depth > self.max_depth or not self._budget_left():
            return
        _, views, _ = self._await_stable()
        if not views:
            return
        enabled = {}
        elements = self._enumerate_scrolled(views, enabled)
        if not elements:
            return

        if not skip_tabs:
            strip = self._dedupe(self._tab_strip(elements, views))
            # A strip already handled is not a new one. The mode bar is drawn
            # on every screen, so without this every ordinary descent -- which
            # runs with skip_tabs False -- re-detected it, emitted the modes
            # again at that depth and entered them, giving
            # `PORTRAIT > Quick controls > PHOTO > ...` and max_depth 19.
            # Only tabs never seen before open a new level.
            strip = [t for t in strip
                     if t.label.strip().lower() not in self._tab_labels]
            if len(strip) >= 2:
                self._tab_labels |= {t.label.strip().lower() for t in strip}
                for tab in strip:
                    if not self._budget_left():
                        return
                    # Each tab, then everything inside it, before the next
                    # tab. Emitting all four first put PORTRAIT, PHOTO, VIDEO
                    # and MORE on consecutive rows, with PORTRAIT's contents
                    # beginning below its own siblings instead of directly
                    # beneath it.
                    self._emit(tab, depth, path, path_selectors)
                    self._enter_tab(tab, depth, path, path_selectors, ancestors,
                                    parent)
                return

        main_twin = self._recorded(path[1:]) if self._pass_marker and len(path) > 1 else None
        here_sig = self._signature(elements)
        same_as_main = bool(main_twin) and (
            len(here_sig & main_twin) / max(1, len(here_sig | main_twin))
            >= self.revisit_similarity)
        # Only when its parent was the main walk's screen too: the front
        # camera's MORE grid is identical to the rear one, but under it PRO
        # and NIGHT differ -- MORE is walked, FOOD (same, under same) is not.
        if same_as_main:
            self._same_as_main.add(tuple(path))
        if self._pass_marker and len(path) > 1 and (
                (same_as_main and tuple(path[:-1]) in self._same_as_main)
                or any(e.label == self._pass_marker for e in elements)):
            # Or the screen IS the main walk's screen for the same path --
            # FOOD and PANORAMA show no switch button at all, but the rear
            # camera's own lenses: the very screen walked in the main tree.
            # The control that started this pass is back on screen: this
            # screen has left the pass's state. Agreed (front camera): FOOD
            # and PANORAMA switch to the rear camera -- a leaf with a note,
            # their tree is in the main walk.
            for row in reversed(self.rows):
                if row.depth == depth - 1 and row.raw_label == path[-1]:
                    row.note = (f"leaves the '{self._pass_marker}' state -- "
                                "walked in the main tree")
                    break
            # And the state stays left: after PANORAMA the camera is the
            # rear one, the MORE grid looks the same, and SLOW MOTION,
            # HYPERLAPSE and PORTRAIT VIDEO were entered on the rear camera
            # and wrongly written off. Replaying the parent's path (which
            # presses the pass's control again) puts the state back.
            self._replay(path[:-1])
            return

        signature = self._signature(elements)
        node = Node(path[-1] if path else self.package, depth, path,
                    path_selectors, elements, signature, entering, parent)
        node.enabled = enabled
        node.list_only = bool(path) and path[-1].strip().lower() in self.list_only
        here = node.where
        outer_dialog = self._in_dialog
        self._in_dialog = looks_like_dialog(views, self.package)
        context = path[0] if path else ""
        seen_at = (self._already_documented(signature, context)
                   if self.skip_documented_screens else None)
        if seen_at is not None:
            logger.info("visit  depth %-2d  %-38s  already listed under %s",
                        depth, here, seen_at)
            self._reused_screens += 1
            return
        self._documented[(context, here)] = signature

        # The same screen walked before under another branch?
        reopened_before, seen_under = set(), None
        if self.revisits != "full" and path:
            # Not across a pass at mode level: the front camera's MORE grid
            # matches the rear one, and as a revisit none of its modes were
            # opened -- the very thing the pass is for. Below the modes
            # (Settings) a revisit is a revisit.
            in_pass = bool(self._pass_marker) and path[:1] == [self._pass_marker]
            own_depth = len(path) - (1 if in_pass else 0)
            for earlier_path, earlier in self._walked:
                earlier_in_pass = bool(self._pass_marker) and earlier_path[:1] == [self._pass_marker]
                if earlier_in_pass != in_pass and own_depth <= 2:
                    continue
                # Another route to it: same screen name, different way in.
                # (Not "a different first step": MORE > PRO and MORE > NIGHT
                # both begin with MORE.)
                if (earlier_path[-1:] == path[-1:] and earlier_path != path
                        and len(signature & earlier) / max(1, len(signature | earlier))
                        >= self.revisit_similarity):
                    reopened_before, seen_under = earlier, " > ".join(earlier_path)
                    break
        if seen_under is not None:
            self._revisited += 1
            if self.revisits == "note":
                self._emit(self._synthetic(f"Same as {seen_under}"), depth, path,
                           path_selectors, note="--revisits note: listed once")
                return
        self._walked.append((list(path), signature))

        self._nodes_visited += 1
        logger.info("visit  depth %-2d  %-38s  %d element(s)",
                    depth, here, len(elements))

        # Strip members are chrome here, wherever "here" is. They are already
        # nodes near the root, so they are neither listed again nor pressed.
        def is_tab(element):
            return element.label.strip().lower() in self._tab_labels

        chrome = {self._fingerprint(e) for e in elements if is_tab(e)}
        chrome |= {self._fingerprint(e)
                   for e in self._tab_strip(elements, views)}
        handled = set(chrome)
        # A control carried over from a screen above, when this screen has a
        # copy of its own -- same label, kind and id, in a different list.
        # The camera's top bar keeps `Flash` and `Lighting` on screen while
        # the Quick controls panel shows its own `Flash` and `Lighting`; the
        # sheet lists the panel's, in the panel's order.
        inherited = set().union(*ancestors) if ancestors else set()
        node.inherited = inherited
        node.own = {self._fingerprint(e) for e in elements
                    if self._fingerprint(e) not in inherited and not is_tab(e)
                    and not self._is_back(e)} if inherited else set()
        if inherited:
            own = {(e.label, e.kind, e.selector_value) for e in elements
                   if self._fingerprint(e) not in inherited}
            twins = {self._fingerprint(e) for e in elements
                     if self._fingerprint(e) in inherited
                     and (e.label, e.kind, e.selector_value) in own}
            handled |= twins
            if twins:
                logger.info("carried over from above, not walked here: %s",
                            sorted(t[0] for t in twins))
        # A screen that shows a carousel that selects gets the carousel walk,
        # however it was reached: the front camera's Quick controls > Color
        # tone opened as a screen, and its items were pressed as menu items
        # -- selected one after another, never put back.
        carousel = self._selector_in(views) if entering is not None else None
        if carousel is not None and only is None:
            handled |= {self._fingerprint(e) for e in elements if e.group == carousel[1]}
            self._walk_selector(carousel, entering, depth, path, path_selectors,
                                node, inherited)
        switches_done = set()
        to_walk = ([e for e in elements if self._fingerprint(e) in only]
                   if only is not None else elements)

        try:
            # One scroll sweep per screen, then work down what it found.
            #
            # The loop used to re-enumerate WITH SCROLLING on every pass, so
            # each click paid to re-scroll the whole list again. Measured:
            # 1447 scrolls for 582 clicks, and at ~1.6s per scroll that was
            # the entire two-hour budget spent dragging lists. The walk never
            # reached the Settings tree at all, and rows it had found on
            # earlier runs -- Video stabilization, Dual recordings, Shooting
            # methods, About Camera -- came back missing.
            #
            # The sweep already knows everything on the screen. Re-reading it
            # per click bought nothing except the chance to notice new
            # arrivals, and those are caught by `revealed` and by the backstop.
            # `only`: walk just these elements of the screen -- what a switch
            # revealed -- while the WHOLE screen still identifies the node.
            for element in to_walk:
                if not self._budget_left():
                    return
                fingerprint = self._fingerprint(element)
                if fingerprint in handled:
                    continue
                handled.add(fingerprint)

                why = self._worth_pressing(element)
                if why is None and self.revisits != "full" and element.label in self._opens_app:
                    # The same app, already listed once: the Gallery from
                    # every mode. Coming back from it also drops the camera
                    # into PHOTO, so each visit cost a relaunch.
                    package, where = self._opens_app[element.label]
                    why = f"opens {package}; listed under {where}"
                if why is None and seen_under is not None and fingerprint in reopened_before:
                    why = f"listed; opened under {seen_under} (--revisits top)"
                if why is None and node.list_only:
                    why = "listed, not pressed (profile: list only)"
                if why is not None:
                    self._emit(element, depth, path, path_selectors, note=why)
                    continue

                # Roles that are never pressed are decided from what was
                # seen, before any attempt to locate them.
                if element.role == "state":
                    continue                    # written as the Off / On rows
                fixed_notes = {"summary": "summary -- listed, not pressed",
                               "inline": "switch of the row above",
                               "option": "option -- listed, not selected",
                               "checkbox": "checkbox -- listed, not ticked"}
                if element.role in fixed_notes:
                    self._emit(element, depth, path, path_selectors,
                               note=fixed_notes[element.role])
                    continue
                # One switch, operated once. A switch bar is a container, a
                # label, an "Off" text and the switch itself; each was being
                # treated as a switch row, and Off/On was written five times.
                if element.switch_key and element.switch_key in switches_done:
                    self._emit(element, depth, path, path_selectors,
                               note="switch of the row above")
                    continue

                _, views, _ = self._await_stable()
                if not views:
                    return
                current = self._elements(views)
                # Still on this node? Containment (_is_node) is the whole test:
                # it already accepts a scrolled list, because it asks what
                # share of what is visible belongs here.
                #
                # It used to be OR'd with "is the current scrollable on the
                # current screen" -- which is always true, so any screen with
                # a list passed as this node. After the Watermark switch was
                # walked the walk stood elsewhere and spent the rest of the
                # node failing to find Watermark's controls, then did the
                # same to the bottom of the Settings list.
                if not self._is_node(elements, current, node.own):
                    if not self._return_to(node):
                        logger.warning("drifted off %s and could not get back", here)
                        return
                    _, views, _ = self._await_stable()
                    if not views:
                        return

                # It may be below the fold: scroll to THIS control, rather
                # than re-sweeping the screen to find everything again.
                target, views = self._find_element_scrolled(element.label, views)
                if target is not None:
                    # Two controls can share a label: on Scanning, the
                    # `Document scan` TITLE opens a screen and the `Document
                    # scan` SWITCH flips a setting. Found by label, the switch
                    # resolved to the title, which was pressed a second time
                    # and walked twice. Prefer the one that is the same
                    # control -- same kind, same selector.
                    # Same identity first -- which includes the list it is
                    # in: the top bar's `Flash` and the Quick controls
                    # panel's `Flash` agree on label, kind and id.
                    on_screen = self._elements(views)
                    same = ([e for e in on_screen
                             if self._fingerprint(e) == self._fingerprint(element)]
                            or [e for e in on_screen
                                if e.label == element.label
                                and e.kind == element.kind
                                and e.selector_value == element.selector_value])
                    if same:
                        target = same[0]
                if target is None:
                    # Said out loud. A control the walk cannot locate used to
                    # vanish without a trace, which is how a lookup that could
                    # never succeed spent a whole run scrolling.
                    self._not_located += 1
                    logger.info("could not locate %r on %s", element.label, here)
                    self._emit(element, depth, path, path_selectors,
                               note="listed, could not be located to press")
                    continue

                # What it IS decides what to do with it (docs/HANDLING.md).
                role, row_i, check_i = self._row_role(target, views)
                self._roles[role] = self._roles.get(role, 0) + 1
                notes = {"summary": "summary -- listed, not pressed",
                         "inline": "switch of the row above",
                         "option": "option -- listed, not selected",
                         "checkbox": "checkbox -- listed, not ticked"}
                self._emit(element, depth, path, path_selectors,
                           note=notes.get(role, ""))
                if role in notes:
                    continue
                if role in ("switch", "switch_row"):
                    if target.switch_key:
                        switches_done.add(target.switch_key)
                    if element.switch_key:
                        switches_done.add(element.switch_key)
                    tap_i = check_i if role == "switch" else row_i
                    governed = self._operate_switch(
                        target, views, tap_i, check_i, depth, path,
                        path_selectors, node, ancestors + [signature])
                    # Already written under `On`; not again as siblings.
                    handled |= governed
                    continue
                # A control named as the screen it is on -- or a screen above
                # it -- is a title. FOOD's `FOOD` label is pressable, also
                # from FOOD's Quick controls, and leaves FOOD for PHOTO, from
                # where the system Back closes the camera.
                if path and target.label.strip().lower() in {
                        x.strip().lower() for x in path}:
                    self._titles_not_pressed += 1
                    continue
                # The screen as it stands RIGHT NOW, after any scrolling done
                # to reach this control. Both the before-signature and the
                # moved-or-not test have to use this same snapshot.
                #
                # They did not: `moved` was measured against a view captured
                # before the scroll, so pressing Flash -- which expands its
                # options in place and moves nothing -- looked like a descent.
                # The walk then tried to descend, the ancestor-similarity
                # guard refused it as a loop, and BACK_FLASH_OFF/AUTO/ON were
                # recorded nowhere. A working behaviour disappeared because an
                # unrelated change moved the baseline out from under it.
                standing = self._elements(views)
                before = self._signature(standing)
                if not self._click(target, views):
                    continue
                _, after_views, after_pkg = self._after_press(label_set(standing))
                if not after_views:
                    continue
                after = self._elements(after_views)
                child_path = path + [target.label]
                child_selectors = list(path_selectors) + [
                    (target.selector_kind, target.selector_value)
                    if target.selector_kind else ("text", target.label)]

                if after_pkg and after_pkg != self.package:
                    # Another app: its first screen is listed one level down
                    # (agreed: EXPERT RAW's Store page, Bixby Vision's
                    # permission prompt), nothing in it pressed or answered,
                    # and the walk comes back.
                    self._foreign_skipped += 1
                    self._opens_app[target.label] = (after_pkg, " > ".join(child_path))
                    logger.info("foreign screen %s -- listed one level", after_pkg)
                    time.sleep(0.8)
                    _, there, there_pkg = self._await_stable()
                    shown = self._dedupe_controls(
                        [e for e in self._elements(there or [])])
                    for e in shown:
                        self._emit(e, depth + 1, child_path, child_selectors,
                                   note=f"in {there_pkg or after_pkg} -- listed, not pressed")
                    self._return_to(node)
                    continue

                if target.label.strip().lower() in self.list_only:
                    # Agreed (MORE > Edit): list what it shows -- all of it,
                    # even rows that look like the screen below -- press
                    # nothing, step back.
                    time.sleep(0.6)
                    _, there, _ = self._await_stable()
                    for e in self._dedupe_controls(self._elements(there or [])):
                        if self._fingerprint(e) in inherited or e.label == target.label:
                            continue
                        self._emit(e, depth + 1, child_path, child_selectors,
                                   note=("back control" if self._is_back(e) else
                                         "listed, not pressed (profile: list only)"
                                         if e.interactive else ""))
                    # Always step back out. Edit shows the very tiles of the
                    # MORE grid, so it passed for MORE: the walk stayed in
                    # Edit and pressed PRO there ("Touch and hold to move").
                    self.driver.press_back(settle=False)
                    self._after_press(label_set(self._elements(there or [])))
                    _, back, _ = self._await_stable()
                    if not self._is_node(elements, self._elements(back or []), node.own):
                        self._return_to(node)
                    continue

                flipped = self._relabelled(target, views, after_views)
                if flipped is None and screen_similarity(standing, after) >= self.similarity_threshold:
                    # A toggle can show a toast first and relabel a moment
                    # later: Quick controls' stabilisation chip was read in
                    # between, and stabilisation was left on.
                    time.sleep(0.6)
                    _, later, _ = self._await_stable()
                    if later:
                        flipped = self._relabelled(target, views, later)
                        if flipped is not None:
                            after_views, after = later, self._elements(later)
                if flipped is not None:
                    self._undo_relabel(target, flipped, depth, child_path,
                                       child_selectors)
                    handled.add(self._fingerprint(flipped))
                    continue

                moved = screen_similarity(standing, after) < self.similarity_threshold
                if moved and not any(self._fingerprint(e) not in before for e in after):
                    # Things went, nothing came -- yet. Filters clears the top
                    # bar before its carousel slides in; read in between, the
                    # press "brought nothing" and 124 rows were never walked.
                    time.sleep(1.0)
                    _, later, later_pkg = self._await_stable()
                    if later:
                        after_views, after = later, self._elements(later)
                        moved = screen_similarity(standing, after) < self.similarity_threshold
                # Everything the press brought onto the screen, and separately the
                # part of it that is pressable.
                #
                # Only the pressable half used to be treated as revealed, so the
                # title and the explanatory line a panel brings with it fell
                # through to this node's own listing and were recorded as
                # siblings of the control that opened them: "Select picture to
                # use as filter" and "Filter Name edit" came out at depth 3 under
                # PHOTO, where the sheet has them at 5 and 6.
                #
                # A caption arrives because of the press, exactly as a button
                # does, and belongs to the same node.
                appeared = [e for e in after if self._fingerprint(e) not in before]
                revealed = [e for e in appeared if e.interactive]
                # What is genuinely NEW: not on this screen, and not on a
                # screen above it coming back into view. Flash and Timer in
                # Quick controls close the panel and show their options in
                # the top bar; the rest of the camera screen returning with
                # them is the camera screen, not Flash's children.
                fresh = [e for e in appeared if self._fingerprint(e) not in inherited]
                back_above = any(
                    len(self._signature(after) & seen) / max(1, len(after)) >= self.node_containment
                    for seen in ancestors)
                # Or, needing no record of the screen above: the control that
                # OPENED this screen has come back into view. After a reset
                # the camera screen recorded on the way in carried a first-
                # launch tip, matched nothing later, and Flash's options were
                # walked as a new screen -- into Filters, pressing filters.
                if path and not back_above:
                    back_above = any(e.interactive and e.label == path[-1]
                                     for e in appeared)

                # A panel, not a new screen: the press left the walk on this
                # screen, or back on the one above it -- never a screen of its
                # own, which Photo enhancer's radio list is.
                # A carousel that selects arriving with the press is a panel
                # on this screen too, however much else moved: opening PHOTO's
                # Filters hides the zoom row, read as a new screen, and its
                # filters were pressed like menu items -- selected, not put back.
                brings_selector = False
                if fresh:
                    found_sel = self._selector_in(after_views)
                    brings_selector = found_sel is not None and any(
                        e.group == found_sel[1] for e in fresh)
                # Or the screen's carried-over parts are still there. A panel
                # leaves the camera's top bar and shutter in place (Face and
                # Exposure in PHOTO kept 7 of 12; Timer 10 of 10); a screen of
                # its own replaces them all (Settings kept none). Face's panel
                # hid the chip row and the mode strip, read as a new screen,
                # and its items were pressed one by one.
                carried = before & inherited
                keeps = (len(carried) >= 3 and
                         len(carried & self._signature(after)) >= 0.5 * len(carried))
                # Or nothing it brought is worth walking: only options, back
                # controls and readings (PRO VIDEO's Mic: six sources, a level,
                # Close). Walked as a screen, each source was pressed -- and
                # the screen around it listed again under every one.
                # ...and much of the screen is still there around it. A
                # Settings sub-screen of radios (Photo enhancer) also brings
                # only options, but replaces the list: a screen of its own,
                # with its own Navigate up row.
                settled = bool(fresh) and any(e.interactive for e in fresh) and all(
                    e.role == "option" or self._is_back(e) or not e.interactive
                    for e in fresh) and (
                    len(before & self._signature(after)) >= 0.3 * max(1, len(before)))
                # But a press that brought controls that themselves need
                # walking opened a screen, however much stayed around it:
                # Quick controls keeps the camera's top bar and shutter in
                # place too, and was listed as a panel -- its chips, and the
                # Settings behind them, never walked. Options, back controls,
                # state toggles and readings need no walking.
                states = [self._state_of(e, after_views) for e in fresh]
                readouts = {state for _, state in filter(None, states)}
                # Clickable in its own right: a panel's title ("Video size")
                # and a live reading ("1/30s") are pressable only through
                # their container, and are not controls to walk.
                walkable = [e for e in fresh if e.interactive and e.role != "option"
                            and not self._is_back(e) and e.label not in readouts
                            and 0 <= e.view_index < len(after_views)
                            and after_views[e.view_index].get("clickable")
                            and self._state_of(e, after_views) is None]
                # Back on the screen above is a panel whatever came with it:
                # PRO VIDEO's Video size closes Quick controls, and PRO
                # VIDEO's own ISO / SPEED / EV -- live values, so "new" --
                # were taken for controls of a new screen.
                in_place = (brings_selector or not moved or back_above
                            or (not walkable and (keeps or settled
                                                  or self._is_node(elements, after))))
                logger.info("press %r: moved=%s back_above=%s in_place=%s "
                            "appeared=%d fresh=%d", target.label, moved,
                            back_above, in_place, len(appeared), len(fresh))
                if fresh and in_place:
                    if not moved and not any(e.interactive for e in fresh):
                        # Only a caption came up: Lighting, Night shot.
                        self._list_captions(fresh, target, depth, path,
                                            path_selectors, handled)
                    else:
                        # Options, or a dial. Not "any interactive": Face's
                        # dial was read while its Close was still animating
                        # in and not yet clickable, and was walked as a screen.
                        self._list_panel(fresh, target, depth, child_path,
                                         child_selectors, node, elements, handled,
                                         before, inherited)
                    continue

                if not moved and revealed:
                    # An expansion in place: Flash stays put and On/Off/Auto
                    # appear beside it. The sheet lists them as children of Flash
                    # and does not select one, because selecting one changes the
                    # camera rather than exploring it.
                    for option in self._dedupe(appeared):
                        why = self._worth_pressing(option)
                        self._emit(option, depth + 1, child_path, child_selectors,
                                   note=("option -- listed, not selected"
                                         if why is None else why))
                        if why is None:
                            self._options_listed += 1
                    for option in appeared:
                        handled.add(self._fingerprint(option))
                    continue

                if not moved:
                    # Nothing came -- but did the screen's own controls go?
                    # Then the press changed the app's state. "Use Night
                    # mode", a suggestion the camera shows in dim light, put
                    # PHOTO into night mode: Flash, Resolution, Motion photo
                    # and Filters vanished, nothing new appeared, and the
                    # rest of PHOTO was walked in the wrong state. A clean
                    # relaunch to this screen puts the state back.
                    here_now = self._signature(after)
                    lost = [fp for fp in (before & node.own if node.own else before)
                            if fp not in here_now and fp != self._fingerprint(target)]
                    if len(lost) >= 2 and not appeared:
                        logger.warning("pressing %r removed %d of this screen's controls "
                                       "and showed nothing: state changed, relaunching",
                                       target.label, len(lost))
                        self._emit_note(target, depth, "changes the app's state -- "
                                        "undone by relaunching")
                        self._states_reset += 1
                        self._replay(path)
                    continue        # the press did nothing visible

                self._descents += 1

                # No menu reaches itself through its own name. The similarity
                # check below catches a return to a screen that LOOKS the
                # same; this catches a loop whose laps each differ slightly
                # and slip under the threshold. Without it one run produced
                # `... > Switch to front camera > Capture > Capture > Capture`
                # and filled depths 9 to 14 with twelve rows apiece.
                #
                # Written for the older walker and never copied here; raising
                # --max-depth simply gave the loop more room.
                if target.label in path:
                    self._loops_refused += 1
                    self._return_to(node)
                    continue

                child_signature = self._signature(after)
                # Landed back on a screen we came THROUGH? Containment, as in
                # _is_node: what share of the new screen belongs to an
                # ancestor. Jaccard was used here and failed exactly as it did
                # for node recognition -- one screenful of Settings against the
                # whole swept list scores low. So when a date spinner under
                # Watermark > On closed the screen and dropped the walk back
                # on the Settings list, that read as a NEW child, and the
                # lower half of Settings was documented under
                # `Watermark > On > spinner_layout` at depth 8, and then again,
                # correctly, at depth 5.
                if any(len(child_signature & seen) / max(1, len(child_signature))
                       >= self.node_containment
                       for seen in ancestors + [signature]):
                    self._loops_refused += 1
                    self._return_to(node)
                    continue

                self._visit(depth + 1, child_path, child_selectors,
                            ancestors + [signature], entering=target, parent=node)
                if not self._return_to(node):
                    # One lost step back used to end this node AND, through
                    # the same check in each caller, every node above it.
                    # A single toggle on the Settings screen took out depth
                    # 6, 5 and 4 in one cascade. Try once more to stand on
                    # this screen again; give up on the node only if that
                    # fails too.
                    logger.warning("could not step back to depth %d (%s)", depth, here)
                    if not (self._replay(path) and self._on_screen(elements)):
                        return
                    self._recovered += 1
        finally:
            self._in_dialog = outer_dialog
            # Whatever this screen had that the walk never got to. The loop
            # emits each row as it reaches it, which is what puts a control's
            # children directly beneath it -- but it also means an early exit
            # loses everything after the point it stopped. `MORE > Filters`
            # was entered with 36 elements on it and recorded none, because a
            # drift check ended the node before the first emit.
            #
            # Order first, completeness guaranteed: the leftovers go in at
            # this depth, in document order, saying plainly that they were
            # seen and not opened.
            for element in to_walk:
                fingerprint = self._fingerprint(element)
                if fingerprint in handled:
                    continue
                handled.add(fingerprint)
                why = self._worth_pressing(element)
                self._emit(element, depth, path, path_selectors,
                           note=why or "listed, not opened -- "
                                       "the walk left this screen first")
                self._left_unopened += 1

    def _enter_tab(self, tab, depth, path, path_selectors, ancestors, parent=None):
        """Switch to a tab and walk what is inside it.

        Tabs need no stepping back: they are siblings, so the way out of one
        is to press the next. Pressing BACK here would leave the app.
        """
        _, views, _ = self._await_stable()
        if not views:
            return
        live = next((e for e in self._enumerate_scrolled(views)
                     if e.label.strip().lower() == tab.label.strip().lower()), None)
        if live is None or not self._click(live, views):
            return
        selectors = list(path_selectors) + [
            (tab.selector_kind, tab.selector_value) if tab.selector_kind
            else ("text", tab.label)]
        self._visit(depth + 1, path + [tab.label], selectors, ancestors,
                    skip_tabs=True, entering=tab, parent=parent)

    def _is_node(self, node_elements, current, own=None) -> bool:
        """Is the screen showing now part of this node?

        Containment, not overlap. A long list is recorded as a node by
        scrolling it end to end -- 47 rows for the camera's Settings -- but
        returning to it shows ONE screenful, about 20. A Jaccard comparison of
        those is 20/47, about 0.43, below the 0.55 bar: the walk stood on the
        Settings screen and concluded it was somewhere else. Every step back
        to a scrolled list failed that way, so each branch was abandoned
        after its first sub-screen: `Scanning > Document scan` was walked and
        then nothing else under Settings ever was.

        The right question is what share of what is visible now belongs to
        the node. A sub-screen fails it because most of its rows are its own.
        """
        here = label_set(current)
        if not here:
            return False
        known = label_set(node_elements)
        if len(here & known) / len(here) < self.node_containment:
            return False
        # And at least one of the node's OWN controls must show. A panel
        # opened over the camera screen shares most of what is visible with
        # it: closed, the camera screen still passed as the panel, and the
        # next chip, Timer, was pressed from the camera screen and walked as
        # a screen of its own.
        if own:
            return any(self._fingerprint(e) in own for e in current)
        return True

    def _on_screen(self, elements) -> bool:
        _, views, package = self._await_stable()
        if not views or (package and package != self.package):
            return False
        return self._is_node(elements, self._elements(views))

    def _find_element(self, label, views, rid=None):
        """Find a control by label -- whether or not the dump calls it clickable.

        The walk presses everything, interactive or not, because Android hangs
        the click on a row's container and reports the visible text as
        unclickable. The LOOKUP was never told: it still searched interactive
        elements only. So for every header and caption on the Settings screen
        -- `Camera settings [Title]`, `Intelligent features` -- it found
        nothing on screen, scrolled the whole list down and back up looking
        (about 100s each), found nothing again, and moved on. That was the
        endless scrolling on Settings, and it used the time the real menu
        items needed.

        Interactive matches are still preferred, so a label shared by a
        caption and its button resolves to the button.
        """
        found = super()._find_element(label, views, rid)
        if found is not None:
            return found
        return next((e for e in self._elements(views) if e.label == label), None)

    def _await_label(self, label, views, timeout: float = 5.0):
        """Wait for a control to appear before deciding it needs scrolling.

        A replay presses a step and immediately looks for the next one. A mode
        switch animates, and the screen read in that window is transitional:
        after pressing PORTRAIT the next lookup saw neither the old screen
        nor the new one, missed `Quick controls`, and fell through to
        scrolling -- and a swipe across the camera preview SWITCHES MODE, so
        the replay ended back in PHOTO having failed on a control that was
        on screen a second later.

        Polling first means scrolling is only tried for a control that is
        genuinely below the fold.
        """
        deadline = time.time() + timeout
        current = list(views or [])
        while True:
            element = self._find_element(label, current) if current else None
            if element is not None:
                return element, current
            if time.time() >= deadline:
                return None, current
            time.sleep(0.4)
            _, fresh, _ = self._await_stable()
            current = list(fresh or [])

    def _after_press(self, before_labels, timeout: float = 3.0):
        """The screen once a press has had time to take effect.

        Read straight after a tap, the screen is often still the old one:
        a new screen slides in, a pop-up list fades in. The walk then
        concluded the press did nothing and moved on -- which is why
        `Pro mode picture format` (a screen of three formats) and the font
        list under Watermark (Default / Roboto / Noto Serif) were never
        written. Wait for the labels to change, up to `timeout`; a press that
        genuinely does nothing costs the full wait, once.
        """
        deadline = time.time() + timeout
        while True:
            key, views, package = self._await_stable()
            views = views or []
            if label_set(self._elements(views)) != before_labels:
                return key, views, package
            if time.time() >= deadline:
                return key, views, package
            time.sleep(0.4)

    def _settled(self, label, views, timeout: float = 3.0):
        """Wait until `label` stops MOVING, then return (element, views).

        An entering screen slides or fades in. Its labels are readable from
        the first frame, so a lookup succeeds at once -- and a tap at that
        moment lands while the view is still in motion and is ignored. The
        replay pressed `Photo enhancer` that way and stayed on the Settings
        list. An animation moves bounds, not text, so the test is the
        control's own bounds being identical in two consecutive dumps.
        """
        deadline = time.time() + timeout
        last = None
        element = self._find_element(label, views) if views else None
        while element is not None and time.time() < deadline:
            bounds = views[element.view_index].get("bounds")
            if bounds == last:
                return element, views
            last = bounds
            time.sleep(0.35)
            _, views, _ = self._await_stable()
            views = views or []
            element = self._find_element(label, views)
        return element, views

    def _replay(self, path, passed=None, launch=True) -> bool:
        """Relaunch and click down `path`, scrolling to reach each step.

        `_relaunch_and_replay` verifies where it lands against a known state
        key and finds each step with `_find_element`, which does not scroll.
        Neither suits the second pass: there is no target key to verify
        against, and the controls worth retrying are mostly the ones that
        were below the fold to begin with. Passing an empty key made every
        replay report failure, so the first second pass reopened 0 of 243.
        """
        assert self.driver is not None
        if launch:
            self._relaunches += 1
            try:
                if not self.driver.launch_clean(self.package,
                                                clear=self.clear_between_paths):
                    self.driver.start_app(self.package,
                                          clear=self.clear_between_paths)
            except DriverError:
                return False
            if self.clear_between_paths and self.first_launches:
                # A wiped app is a first launch again: bring it to the state
                # the walk runs in (agreed: Location granted), or the
                # location prompt and tips come back mid-walk.
                self._prime(wipe=False)
        _, views, _ = self._await_stable()
        if not views:
            return False
        for step, label in enumerate(path):
            switch_step = self._switch_steps.get(tuple(path[:step + 1]))
            if switch_step is not None:
                # `On` is not a control: it is the state of the switch whose
                # rows are being walked. Replaying to Watermark > On > Edit
                # looked for a button called On, found none, and the rest of
                # what Watermark governs -- Time, Font, Alignment -- was
                # walked with the switch off, so nothing opened. Set the
                # switch instead; the walk restores it when it is done.
                if not self._set_switch(*switch_step):
                    logger.info("replay could not set the switch for %r of %s",
                                label, " > ".join(path))
                    return False
                _, views, _ = self._await_stable()
                continue
            if not self._tab_labels and views:
                # Learn the mode strip here if the walk could not at start-up:
                # after a reset a first-launch pop-up covers the launch screen,
                # the strip went unlearned, and a sweep below swiped it --
                # turning PORTRAIT into PHOTO under the walk's feet.
                strip = self._dedupe(self._tab_strip(self._elements(views), views))
                if len(strip) >= 2:
                    self._tab_labels = {t.label.strip().lower() for t in strip}
            if passed is not None and views and step:
                # Not the launch screen: after a reset the first launch shows
                # the Quick controls panel under a tip, and recording that as
                # an ancestor made the panel's own chips look carried over.
                #
                # The screens a branch walk starts beneath are its ancestors:
                # a press that drops back onto one of them is a return, not a
                # new child (see the loop guard in _visit). Only a VERTICAL
                # list is swept for it: a horizontal strip on a camera screen
                # is the mode bar, and swiping it changes mode.
                span = self._swipe_span(views)
                seen = self._signature(
                    self._enumerate_scrolled(views) if span and span[0] == "v"
                    else self._elements(views))
                # And once more a moment later: a camera screen's top bar
                # fills in after the mode has switched, and a `Flash` missing
                # from this record was later taken for a control of the screen
                # below -- listed there twice.
                time.sleep(1.0)
                _, views, _ = self._await_stable()
                views = views or []
                passed.append(seen | self._signature(self._elements(views)))
            element, views = self._await_label(label, views)
            if element is not None and self._tap_point(views, element.view_index) is None:
                element = None              # on screen, but covered: scroll to it
            if element is None:
                element, views = self._find_element_scrolled(label, views)
            if element is None and self._clear_blocking_dialog():
                # A cleared app greets every launch with "Turn on Location
                # tags?", and the first step of any path is behind it. The
                # walk proper treats that prompt as part of the tree; a
                # replay is only trying to get somewhere, so it declines it.
                # Without this every replay after a reset failed at step one.
                _, views, _ = self._await_stable()
                element, views = self._find_element_scrolled(label, views or [])
            if element is not None:
                element, views = self._settled(label, views)
            if element is not None:
                before = label_set(self._elements(views))
            if element is not None:
                self._replayed_last = element   # how the last screen was entered
            if element is None or not self._click(element, views):
                _, seen, package = self._await_stable()
                logger.info("replay stopped at %r of %s; screen (%s) had: %s",
                            label, " > ".join(path), package,
                            [e.label for e in self._elements(seen or [])][:24])
                return False
            # Wait for the press to take effect before reading the screen.
            # Returning straight after the last tap read the PREVIOUS screen:
            # a replay to Scanning reported success while the Settings list
            # was still showing, and the node built from that was wrong.
            #
            # A tap can also be swallowed outright: the next step's label is
            # on screen the moment a list renders, while its entry animation
            # is still running, and a tap then is ignored. So an unchanged
            # screen earns ONE more press of the same control -- and a replay
            # that still cannot see a change reports failure. It used to fall
            # through and report success from the wrong screen.
            changed = False
            for attempt in range(2):
                deadline = time.time() + 4.0
                while time.time() < deadline:
                    _, views, _ = self._await_stable()
                    # Something must APPEAR. A tap that only made things go
                    # away was taken by an overlay: after a reset the first
                    # launch shows a tip, the tap on PORTRAIT dismissed it,
                    # the screen "changed", and Quick controls was then
                    # opened -- and walked -- in PHOTO.
                    if views and label_set(self._elements(views)) - before:
                        changed = True
                        break
                    time.sleep(0.4)
                if changed or attempt:
                    break
                again = self._find_element(label, views or [])
                if again is None or not self._click(again, views):
                    break
                self._replay_retaps += 1
            is_tab = label.strip().lower() in self._tab_labels
            if not changed and is_tab and views and self._tab_confirmed(label, views):
                changed = True      # the mode was already showing
            if not changed or not views:
                logger.info("replay: pressing %r changed nothing", label)
                return False
            if label.strip().lower() in self.walk_separately:
                # A state switch (front camera) is pressed until its own label
                # is gone -- a first-launch tip can take the first tap.
                for _ in range(2):
                    still = next((e for e in self._elements(views) if e.label == label), None)
                    if still is None or not self._click(still, views):
                        break
                    self._after_press(label_set(self._elements(views)), timeout=1.5)
                    time.sleep(1.0)
                    _, views, _ = self._await_stable()
                    views = views or []
                if any(e.label == label for e in self._elements(views)):
                    logger.info("replay could not switch state with %r", label)
                    return False
            if label.strip().lower() in self._tab_labels:
                # A mode tab is confirmed, and pressed again until it is.
                # After a reset a first-launch tip sits over the camera; the
                # tap on PORTRAIT only closed it, and the walk went on -- and
                # pressed filters -- in PHOTO. Pressing the mode already shown
                # does nothing, so pressing again is free.
                for _ in range(3):
                    if self._tab_confirmed(label, views):
                        break
                    again = self._find_element(label, views)
                    if again is None or not self._click(again, views):
                        break
                    self._after_press(label_set(self._elements(views)), timeout=1.5)
                    _, views, _ = self._await_stable()
                    views = views or []
                else:
                    logger.info("replay could not confirm the mode %r", label)
                    return False
            # Where the walk has been before, the screen must look like it.
            # After a reset the press on the FOOD tile was taken by the
            # grid's first-launch state, the next step `Quick controls` was
            # found anyway -- on PHOTO -- and PHOTO's panel was taken for
            # FOOD's. One more press; then the replay says it failed.
            known = self._recorded(path[:step + 1])
            if known and not self._looks_like(known, views):
                again = self._find_element(label, views)
                if again is not None and self._click(again, views):
                    self._after_press(label_set(self._elements(views)))
                    time.sleep(1.0)
                    _, views, _ = self._await_stable()
                    views = views or []
                if not self._looks_like(known, views):
                    # Said, not enforced: a screen walked before can differ
                    # after a reset (first-launch state), and refusing it
                    # turned a good replay of PHOTO > Quick controls into a
                    # lost branch. Whoever asked for the replay checks where
                    # it landed.
                    logger.info("replay: after %r the screen differs from the "
                                "one walked before", label)
        return True

    def _recorded(self, path):
        """The signature the walk recorded for the screen at `path`."""
        for earlier_path, signature in self._walked:
            if earlier_path == list(path):
                return signature
        return None

    def _looks_like(self, signature, views) -> bool:
        """Is most of what is visible part of that recorded screen?"""
        here = {self._fingerprint(e) for e in self._elements(views)}
        return bool(here) and len(here & signature) / len(here) >= 0.5

    def _tab_confirmed(self, label, views) -> bool:
        """Does the tab strip say that `label` is the tab showing?

        Modes carry no `selected` flag. The strip that holds them announces
        the current one instead -- the camera's reads "Portrait, Mode" or
        "Photo, Mode". A strip that says nothing cannot be asked, and then
        the press is taken on trust.
        """
        tabs = [e for e in self._elements(views)
                if e.label.strip().lower() in self._tab_labels]
        for e in tabs:
            i = views[e.view_index].get("parent")
            while i is not None and 0 <= i < len(views):
                said = (views[i].get("content_description") or views[i].get("text") or "").lower()
                if said:
                    return label.strip().lower() in said
                if views[i].get("scrollable"):
                    break
                i = views[i].get("parent")
        strip = scrollable_container(views)
        said = ((strip or {}).get("content_description") or "").lower()
        return (label.strip().lower() in said) if said else True

    # -- second pass -----------------------------------------------------
    def _reopen_leftovers(self) -> None:
        """Go back for the screens the walk had to abandon.

        A node that loses its screen mid-way -- a dialog steals focus, a
        return fails, a list scrolls under it -- gives up on the rest of that
        screen, and the backstop records the remainder as listed-but-unopened.
        On one run that was 206 rows, concentrated in exactly the places the
        hand-authored tree goes deepest: 35 under
        `PORTRAIT > Quick controls > Go to Settings`, 38 under
        `MORE > Switch to front camera > Quick controls`.

        Time is not what stopped that run -- it ended on its own after 1322s
        of a 2400s budget. The walk simply had nothing left it was willing to
        try. So retry each abandoned control from a clean start: relaunch,
        replay its path, press it, and walk what it opens.

        Every reopened branch is a branch the first pass could not reach, so
        this is the difference between a tree that stops at depth 5 and one
        that reaches the depth the sheet has.
        """
        pending = [row for row in self.rows
                   if row.note.startswith("listed, not opened")]
        if not pending:
            return
        logger.info("second pass: %d control(s) were listed but never opened",
                    len(pending))

        for row in pending:
            if not self._budget_left():
                logger.info("second pass stopped on the clock with %d left",
                            len(pending) - self._reopened)
                return
            if row.depth >= self.max_depth:
                continue
            if not self._replay(row.path):
                continue
            _, views, _ = self._await_stable()
            if not views:
                continue
            here = self._enumerate_scrolled(views)
            live = next((e for e in here
                         if e.label == row.raw_label and e.interactive), None)
            if live is None or self._worth_pressing(live) is not None:
                continue
            before = self._signature(here)
            if not self._click(live, views):
                continue
            _, after_views, after_pkg = self._await_stable()
            if not after_views or (after_pkg and after_pkg != self.package):
                continue
            after = self._elements(after_views)
            if screen_similarity(here, after) >= self.similarity_threshold:
                continue                    # it opened nothing after all
            self._reopened += 1
            row.note = "opened on the second pass"
            selectors = list(row.path_selectors) + [
                (row.selector_kind, row.selector_value)
                if row.selector_kind else ("text", row.raw_label)]
            self._visit(row.depth + 1, row.path + [row.raw_label], selectors,
                        [], entering=live, parent=None)

    def walk(self) -> List[TreeNode]:
        root_key, views = self._start()
        if not root_key:
            return self.rows
        self._root_key = root_key
        if self.first_launches:
            self._walk_first_launches()
            self._prime(wipe=True)
        self._visit(2, [], [], [])
        self._reopen_leftovers()
        self._walk_passes()
        self._release()
        logger.info("Walk finished: %s", self.stats())
        return self.rows

    # -- first launches ---------------------------------------------------
    def _launch(self, wipe=False, wait=4.0):
        """One launch of the app (cleared first if `wipe`); the screen after."""
        try:
            self.driver.launch_clean(self.package, clear=wipe)
        except DriverError:
            return []
        time.sleep(wait)
        _, views, _ = self._await_stable()
        return views or []

    def _press_exact(self, label, wait=3.0):
        """Press the control with exactly this label, on whatever is in front."""
        _, views, _ = self._await_stable()
        hit = next((e for e in self._elements(views or []) if e.label == label), None)
        if hit is None or not self._click(hit, views):
            return False
        time.sleep(wait)
        return True

    def _reach(self, answers):
        """Clear the app, launch it `first_launches` times, give `answers`."""
        views = self._launch(wipe=True)
        for _ in range(self.first_launches - 1):
            views = self._launch()
        for label in answers:
            if not self._press_exact(label):
                return False
        return True

    def _prime(self, wipe):
        """Bring a cleared app to the agreed state: the remaining first
        launches, then the profile's answers -- each one if it is asked."""
        if wipe:
            self._launch(wipe=True)
        for _ in range(self.first_launches - 1):
            self._launch()
        for label in self.launch_answers:
            self._press_exact(label)
        self._await_stable()

    def _dialog_in_front(self, views, package):
        """A pop-up is in front -- not the app's own main screen.

        Against the main screen recorded on the first launch when there is
        one: "Location tags and sharing" fills the window, so the dialog test
        alone took it for the app, and Learn more was written as closing.
        """
        if not views:
            return False
        if package and package != self.package:
            return True
        if self._main_labels:
            here = {e.label for e in self._elements(views)}
            return len(here & self._main_labels) < 0.5 * len(self._main_labels)
        return looks_like_dialog(views, self.package)

    def _read_dialog(self, views):
        """(title, texts, options, buttons) of the pop-up on screen."""
        els = self._dedupe_controls(self._elements(views))
        texts = [e for e in els if not e.interactive]
        options = [e for e in els if e.interactive and e.role == "option"]
        title = texts[0].label if texts else (els[0].label if els else "")
        buttons = [e for e in els if e.interactive and e.role != "option"
                   and e.label != title]            # a tappable title is not an answer
        if not texts and buttons and buttons[0].label == title:
            buttons = buttons[1:]
        return title, texts, options, buttons

    def _walk_first_launches(self):
        """Agreed: the pop-ups of the first launches after clearing the app,
        with every answer branch, under "First launches" before the modes.

        Explored level by level -- every answer to a pop-up tried before any
        answer deeper down -- so that an answer that changes the PHONE (Google
        Play services' Location Accuracy "Turn on", pressed as agreed) comes
        last of all, and a pop-up met twice is recorded once. Rows are then
        written in the sheet's order.
        """
        from .hierarchy import DECLINE_LABELS
        root = Element(label="First launches (app data cleared)", kind="title",
                       interactive=False, view_index=-1)
        self._emit(root, 2, [], [], note="pop-ups after clearing the app (profile: first_launches)")
        launches = []                              # per launch: (tips, dialog_title)
        dialogs = {}                               # title -> record
        start = None                               # (launch number, title)
        def launch_now(wipe):
            # Read straight away, not after the screen settles: the first
            # launch's tip is gone within a few seconds.
            try:
                self.driver.launch_clean(self.package, clear=wipe)
            except DriverError:
                return []
            time.sleep(1.5)
            try:
                return parse_hierarchy(self.driver.dump_hierarchy())
            except Exception:
                return []

        views = launch_now(True)
        for n in range(1, self.first_launches + 1):
            if n > 1:
                views = launch_now(False)
            early = {e.label for e in self._elements(views) if not e.interactive}
            time.sleep(5)
            _, later, package = self._await_stable()
            later = later or []
            tips = [e for e in self._elements(views)
                    if not e.interactive and e.label in early
                    and e.label not in {x.label for x in self._elements(later)}
                    and len(e.label) > 15]
            if n == 1 and not self._main_labels:
                self._main_labels = {e.label for e in self._elements(later)
                                     if e.interactive}
            title = None
            if self._dialog_in_front(later, package):
                title = self._read_dialog(later)[0]
                if start is None:
                    start = (n, title)
            launches.append((tips, title))
            if title:
                break                              # the chain starts here

        if start is not None:
            queue = [[]]
            while queue and self._budget_left():
                answers = queue.pop(0)
                if not self._reach(answers):
                    continue
                _, views, package = self._await_stable()
                if not self._dialog_in_front(views or [], package):
                    continue
                title, texts, options, buttons = self._read_dialog(views)
                if title in dialogs and dialogs[title].get("buttons") is not None:
                    continue
                order = sorted(buttons, key=lambda b: b.label.strip().lower()
                               not in DECLINE_LABELS)
                record = {"texts": texts, "options": options, "buttons": [],
                          "package": package, "path": list(answers)}
                dialogs[title] = record
                for b in order:
                    if not self._reach(answers) or not self._press_exact(b.label):
                        record["buttons"].append((b, ("unreached",)))
                        continue
                    time.sleep(1.0)
                    _, after, pkg = self._await_stable()
                    if self._dialog_in_front(after or [], pkg):
                        nxt = self._read_dialog(after)[0]
                        above = [title] + [d for d, r in dialogs.items()
                                           if r.get("path") is not None
                                           and r["path"] == answers[:len(r["path"])]]
                        if nxt == title or nxt in above:
                            record["buttons"].append((b, ("returns", nxt)))
                        elif nxt in dialogs:
                            record["buttons"].append((b, ("same", nxt)))
                        else:
                            dialogs.setdefault(nxt, {"buttons": None})
                            record["buttons"].append((b, ("dialog", nxt)))
                            queue.append(answers + [b.label])
                    else:
                        record["buttons"].append((b, ("closes",)))
                    self._popup_presses += 1

        # Written in the sheet's order.
        written = set()

        def write_dialog(title, depth, path):
            record = dialogs.get(title) or {}
            self._emit(Element(label=title, kind="title", interactive=False, view_index=-1),
                       depth, path, [], note=(f"in {record.get('package')}"
                                              if record.get("package") and
                                              record.get("package") != self.package else ""))
            if title in written:
                self._emit_note_last("same pop-up as above -- not repeated")
                return
            written.add(title)
            for e in record.get("texts", [])[1:]:
                self._emit(e, depth + 1, path + [title], [])
            for e in record.get("options", []):
                self._emit(e, depth + 1, path + [title], [],
                           note="option -- listed, not selected")
            for b, result in record.get("buttons", []) or []:
                note = {"closes": "closes the pop-up -- back to the app",
                        "unreached": "could not be reached again"}.get(result[0], "")
                if result[0] == "returns":
                    note = f"returns to '{result[1]}'"
                elif result[0] == "same":
                    note = f"opens '{result[1]}' -- the same pop-up as above"
                self._emit(b, depth + 1, path + [title], [], note=note)
                if result[0] == "dialog":
                    write_dialog(result[1], depth + 2, path + [title, b.label])

        for n, (tips, title) in enumerate(launches, start=1):
            self._emit(Element(label=f"Launch {n}", kind="text", interactive=False,
                               view_index=-1), 3, [root.label], [])
            for t in tips:
                self._emit(Element(label=t.label, kind="title", interactive=False,
                                   view_index=-1), 4, [root.label, f"Launch {n}"], [],
                           note="shown on this launch, then closed by itself")
            if title:
                write_dialog(title, 4, [root.label, f"Launch {n}"])

    def _emit_note_last(self, note):
        if self.rows:
            row = self.rows[-1]
            row.note = (row.note + "; " if row.note else "") + note

    def _walk_passes(self):
        """Walk the app again in each state a `walk_separately` control opens.

        Agreed (front camera): the main walk lists the control and never
        presses it; afterwards the app is relaunched, the control pressed,
        and everything walked again under a node named after it -- depth 2,
        its modes at depth 3. The mode strip is forgotten first, so the one
        the new state shows is entered again. A relaunch at the end puts the
        app back as a reset leaves it.
        """
        def exact(label, views):
            # Exactly this label. The forgiving lookup reads "Switch to rear
            # camera" as the same control as "Switch to front camera" once
            # it has flipped, and the switch looked as though it had not.
            return next((e for e in self._elements(views or []) if e.label == label), None)

        for label in self._separate_labels:
            if not self._budget_left():
                return
            if not self._replay([]):
                continue
            _, views, _ = self._await_stable()
            control = exact(label, views)
            if control is None:
                continue                    # not offered here (e.g. "Switch to rear")
            self._emit(control, 2, [], [], note="walked as its own pass (profile)")
            # Pressed until it goes: after the reset a first-launch tip sits
            # over the camera and takes the first tap, as it did the mode tab.
            for _ in range(3):
                standing = self._elements(views or [])
                if not self._click(control, views):
                    break
                self._after_press(label_set(standing))
                time.sleep(1.0)
                _, views, _ = self._await_stable()
                control = exact(label, views)
                if control is None:
                    break
            if exact(label, views) is not None:
                logger.warning("pressing %r did not change the app's state", label)
                continue
            control = Element(label=label, kind="button", interactive=True, view_index=-1)
            logger.info("separate pass: %s", label)
            self._passes_walked += 1
            kept_tabs, self._tab_labels = self._tab_labels, set()
            self._pass_marker = label
            try:
                selector = ((control.selector_kind, control.selector_value)
                            if control.selector_kind else ("text", label))
                self._visit(3, [label], [selector], [], entering=control)
            finally:
                self._pass_marker = None
                self._tab_labels = kept_tabs | self._tab_labels
            self._replay([])                # back as a reset leaves it

    def stats(self) -> Dict:
        base = super().stats()
        base.update({
            "nodes_visited": self._nodes_visited,
            "returns_ok": self._returns_ok,
            "returns_failed": self._returns_failed,
            "returned_by": self._returned_by,
            "options_listed_not_pressed": self._options_listed,
            "panels_listed": self._panels_listed,
            "first_launch_answers_pressed": self._popup_presses,
            "states_reset_by_relaunch": self._states_reset,
            "separate_passes_walked": self._passes_walked,
            "screens_revisited": self._revisited,
            "revisits": self.revisits,
            "selectors_walked": self._selectors_walked,
            "clipped_rows_swept": self._clipped_rows_swept,
            "selections_restored": self._selections_restored,
            "captions_listed": self._captions_listed,
            "presses_undone": self._presses_undone,
            "loops_refused": self._loops_refused,
            "screens_already_documented": self._reused_screens,
            "tab_strips_not_swiped": self._strips_not_swiped,
            "listed_but_not_opened": self._left_unopened,
            "actions_listed_not_performed": self._actions_not_pressed,
            "toggles_listed_not_switched": self._toggles_not_pressed,
            "step_backs_recovered_by_replay": self._recovered,
            "controls_not_located": self._not_located,
            "roles": self._roles,
            "switches_already_on": self._switches_already_on,
            "switches_revealing": self._switches_revealing,
            "switch_dialogs": self._switch_dialogs,
            "switches_that_navigate": self._switches_navigating,
            "guarded_rows_opened": self._guarded_rows_opened,
            "replay_retaps": self._replay_retaps,
            "switches_restored": self._switches_restored,
            "switches_not_restored": self._switches_not_restored,
            "reopened_on_second_pass": self._reopened,
        })
        return base
