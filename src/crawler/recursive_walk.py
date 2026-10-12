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
                 "elements", "entering", "parent", "enabled")

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
        self._in_dialog = False
        self.do_not_press = {str(x).strip().lower()
                             for x in config.get("do_not_press", [])}
        self.skip_documented_screens = bool(
            config.get("skip_documented_screens", False))

    # -- helpers ---------------------------------------------------------
    def _signature(self, elements: Sequence[Element]) -> Set[tuple]:
        return {self._fingerprint(e) for e in elements}

    def _is_back(self, element: Element) -> bool:
        return (element.kind == "back"
                or element.label.strip().lower() in BACK_LABELS)

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
        from .element_tree import KEYPAD_KEY
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
            return self._is_node(node.elements, self._elements(views))

        def press(element: Element, views, how: str) -> bool:
            if self.guard.blocks("text", element.label):
                return False
            if not self._click(element, views):
                return False
            if arrived():
                self._returns_ok += 1
                self._returned_by[how] = self._returned_by.get(how, 0) + 1
                return True
            return False

        _, views, _ = self._await_stable()
        here = self._elements(views) if views else []

        if node.entering is not None:
            wanted = self._fingerprint(node.entering)
            live = next((e for e in here if self._fingerprint(e) == wanted), None)
            if live is not None and press(live, views, "the control that opens it"):
                return True
            _, views, _ = self._await_stable()
            here = self._elements(views) if views else []

        for element in here:
            if self._is_back(element):
                if press(element, views, "on-screen back"):
                    return True
                break

        try:
            self.driver.press_back()
            time.sleep(self.settle)
        except Exception:
            pass
        if arrived():
            self._returns_ok += 1
            self._returned_by["BACK"] = self._returned_by.get("BACK", 0) + 1
            return True

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

    def _fingerprint(self, element):
        """Identity of an element: what it is, AND which row it belongs to.

        Label, kind and selector alone are not enough on a settings list: all
        twenty-odd inline switches are `switch_widget` with one shared id. As
        one identity, the first was handled and every later switch was taken
        as already done -- Video stabilization, Location tags, Shutter sound
        and the rest were written with no switch at all.
        """
        return (element.annotated(), element.kind, element.selector_kind,
                element.selector_value, getattr(element, "context", ""))

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
            out.append(e)
        for e in out:
            role, row, check = self._row_role(e, views)
            e.role = role
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
            if len(sibs) >= 2 and len(labels) == len(sibs) and all(labels):
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
        centre = center_of(views[tap_index])
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
        elif not self._is_node(node.elements, after):
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
        if not self._is_node(node.elements, self._elements(now or [])):
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
        centre = center_of(views[tap_index])
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

    def _headers_of(self, node, governed):
        """Section headers that belong to what a switch governs.

        `Info to show` and `Style` head Watermark's options but are plain
        text: they never change `enabled`, so the switch test leaves them
        behind as siblings of the switch, where the sheet has them under `On`
        (K54 Info to show). A plain label lying among the governed rows, or
        directly above the first of them, heads them. Read from the order of
        the screen, top to bottom -- nothing about the words.
        """
        order = [self._fingerprint(e) for e in node.elements]
        at = [i for i, fp in enumerate(order) if fp in governed]
        if not at:
            return set()
        out = set()
        for i in range(max(0, at[0] - 1), at[-1]):
            e = node.elements[i]
            if order[i] not in governed and not e.interactive and e.role == "plain":
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

    @staticmethod
    def _under_chrome(views, j) -> bool:
        """Is view `j`'s centre covered by the status or navigation bar?

        A sticky switch bar pins itself to the top of a scrolled list, half
        under the status bar: Watermark's sat at [30,0][1050,158] with the
        status bar at [0,0][1080,96]. Its centre was "on screen", and every
        tap at it landed on the status bar -- the restore failed three runs
        running and left Watermark on.
        """
        centre = center_of(views[j])
        if centre is None:
            return True
        x, y = centre
        for v in views:
            if (v.get("package") or "").startswith(CHROME_PACKAGES):
                b = box_of(v)
                if b and b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                    return True
        return False

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
            centre = center_of(views[tap])
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

        signature = self._signature(elements)
        node = Node(path[-1] if path else self.package, depth, path,
                    path_selectors, elements, signature, entering, parent)
        node.enabled = enabled
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
                if not self._is_node(elements, current):
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
                    same = [e for e in self._elements(views)
                            if e.label == element.label
                            and e.kind == element.kind
                            and e.selector_value == element.selector_value]
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
                    self._foreign_skipped += 1
                    logger.info("foreign screen %s -- recorded, not walked", after_pkg)
                    self._return_to(node)
                    continue

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

    def _is_node(self, node_elements, current) -> bool:
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
        return len(here & known) / len(here) >= self.node_containment

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

    def _replay(self, path, passed=None) -> bool:
        """Relaunch and click down `path`, scrolling to reach each step.

        `_relaunch_and_replay` verifies where it lands against a known state
        key and finds each step with `_find_element`, which does not scroll.
        Neither suits the second pass: there is no target key to verify
        against, and the controls worth retrying are mostly the ones that
        were below the fold to begin with. Passing an empty key made every
        replay report failure, so the first second pass reopened 0 of 243.
        """
        assert self.driver is not None
        self._relaunches += 1
        try:
            if not self.driver.launch_clean(self.package,
                                            clear=self.clear_between_paths):
                self.driver.start_app(self.package,
                                      clear=self.clear_between_paths)
        except DriverError:
            return False
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
            if passed is not None and views:
                # The screens a branch walk starts beneath are its ancestors:
                # a press that drops back onto one of them is a return, not a
                # new child (see the loop guard in _visit).
                # Not swept at the launch screen: a swipe across a camera
                # preview, say, changes mode rather than scrolling.
                passed.append(self._signature(
                    self._elements(views) if step == 0 else self._enumerate_scrolled(views)))
                _, views, _ = self._await_stable()
                views = views or []
            element, views = self._await_label(label, views)
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
                    if views and label_set(self._elements(views)) != before:
                        changed = True
                        break
                    time.sleep(0.4)
                if changed or attempt:
                    break
                again = self._find_element(label, views or [])
                if again is None or not self._click(again, views):
                    break
                self._replay_retaps += 1
            if not changed or not views:
                logger.info("replay: pressing %r changed nothing", label)
                return False
        return True

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
        self._visit(2, [], [], [])
        self._reopen_leftovers()
        self._release()
        logger.info("Walk finished: %s", self.stats())
        return self.rows

    def stats(self) -> Dict:
        base = super().stats()
        base.update({
            "nodes_visited": self._nodes_visited,
            "returns_ok": self._returns_ok,
            "returns_failed": self._returns_failed,
            "returned_by": self._returned_by,
            "options_listed_not_pressed": self._options_listed,
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
