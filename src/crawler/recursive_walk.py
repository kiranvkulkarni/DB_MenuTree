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
import time
from typing import Dict, List, Optional, Sequence, Set

from .element_tree import ElementTreeWalker, TreeNode
from .elements import Element, screen_similarity

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
                 "elements", "entering", "parent")

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

    # -- helpers ---------------------------------------------------------
    def _signature(self, elements: Sequence[Element]) -> Set[tuple]:
        return {self._fingerprint(e) for e in elements}

    def _is_back(self, element: Element) -> bool:
        return (element.kind == "back"
                or element.label.strip().lower() in BACK_LABELS)

    def _worth_pressing(self, element: Element) -> Optional[str]:
        """None if it should be pressed, else why it should not be."""
        if not element.interactive:
            return "not interactive"
        if self._is_back(element):
            return "back control"
        from .element_tree import KEYPAD_KEY
        if KEYPAD_KEY.fullmatch(element.label.strip()):
            self._keypad_skipped += 1
            return "keypad key -- recorded, not pressed"
        blocked = self.guard.blocks("text", element.label)
        if blocked:
            return f"action guard: {blocked}"
        return None

    def _emit(self, element: Element, depth: int, path: List[str],
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
            return (screen_similarity(node.elements, self._elements(views))
                    >= self.return_similarity)

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

        if self._relaunch_and_replay("", node.path, clear=self.clear_between_paths):
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

    def _dedupe(self, elements: Sequence[Element]) -> List[Element]:
        """One control per label. The mode strip is rendered twice."""
        seen, out = set(), []
        for element in elements:
            key = element.label.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(element)
        return out

    def _already_documented(self, signature: Set[tuple]) -> Optional[str]:
        """Where this screen was listed before, if it was.

        A common menu is documented ONCE. The deliverable proves that every
        menu and element is reachable; listing the same quick settings again
        under every mode does not add proof, it adds rows.

        The threshold is high -- 0.9, not the 0.55 used for "did navigation
        land where I meant". Photo and Video are both viewfinders and share
        most of their chrome; at 0.55 Video would be skipped as already
        covered, losing a genuine branch.
        """
        for where, seen in self._documented.items():
            overlap = len(signature & seen) / max(1, len(signature | seen))
            if overlap >= self.documented_similarity:
                return where
        return None

    # -- the walk --------------------------------------------------------
    def _visit(self, depth, path, path_selectors, ancestors, skip_tabs=False,
               entering=None, parent=None):
        """List this screen, then walk each item that opens something."""
        if depth > self.max_depth or not self._budget_left():
            return
        _, views, _ = self._await_stable()
        if not views:
            return
        elements = self._enumerate_scrolled(views)
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
                    self._emit(tab, depth, path, path_selectors)
                for tab in strip:
                    if not self._budget_left():
                        return
                    self._enter_tab(tab, depth, path, path_selectors, ancestors,
                                    parent)
                return

        signature = self._signature(elements)
        node = Node(path[-1] if path else self.package, depth, path,
                    path_selectors, elements, signature, entering, parent)
        here = node.where
        seen_at = self._already_documented(signature)
        if seen_at is not None:
            logger.info("visit  depth %-2d  %-38s  already listed under %s",
                        depth, here, seen_at)
            self._reused_screens += 1
            return
        self._documented[here] = signature

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
        listed = set(chrome)
        handled = set(chrome)

        def list_new(current):
            for element in current:
                fingerprint = self._fingerprint(element)
                if fingerprint in listed:
                    continue
                if is_tab(element):
                    listed.add(fingerprint)
                    handled.add(fingerprint)
                    continue
                listed.add(fingerprint)
                why = self._worth_pressing(element)
                self._emit(element, depth, path, path_selectors,
                           note="" if why is None else why)
                if why is not None:
                    handled.add(fingerprint)

        list_new(elements)

        while self._budget_left():
            _, views, _ = self._await_stable()
            if not views:
                break
            current = self._enumerate_scrolled(views)

            # Still on this screen? A stray dialog, a QR overlay, a mode that
            # changed underneath -- without this check their contents are
            # listed as if they belonged here. One run absorbed a QR scanner
            # into the root as depth-2 siblings.
            if screen_similarity(elements, current) < self.similarity_threshold:
                if not self._return_to(node):
                    logger.warning("drifted off %s and could not get back", here)
                    return
                _, views, _ = self._await_stable()
                current = self._enumerate_scrolled(views) if views else []
                if not current:
                    return

            list_new(current)
            target = next((e for e in current
                           if self._fingerprint(e) not in handled), None)
            if target is None:
                break
            handled.add(self._fingerprint(target))

            before = self._signature(current)
            if not self._click(target, views):
                continue
            _, after_views, after_pkg = self._await_stable()
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

            moved = screen_similarity(current, after) < self.similarity_threshold
            revealed = [e for e in after
                        if self._fingerprint(e) not in before and e.interactive]

            if not moved and revealed:
                # An expansion in place: Flash stays put and On/Off/Auto
                # appear beside it. The sheet lists them as children of Flash
                # and does not select one, because selecting one changes the
                # camera rather than exploring it.
                for option in self._dedupe(revealed):
                    self._emit(option, depth + 1, child_path, child_selectors,
                               note="option -- listed, not selected")
                    self._options_listed += 1
                for option in revealed:
                    listed.add(self._fingerprint(option))
                    handled.add(self._fingerprint(option))
                continue

            if not moved:
                continue        # the press did nothing visible

            self._descents += 1
            child_signature = self._signature(after)
            if any(len(child_signature & seen) / max(1, len(child_signature | seen))
                   >= self.return_similarity
                   for seen in ancestors + [signature]):
                self._loops_refused += 1
                self._return_to(node)
                continue

            self._visit(depth + 1, child_path, child_selectors,
                        ancestors + [signature], entering=target, parent=node)
            if not self._return_to(node):
                logger.warning("could not step back to depth %d (%s)", depth, here)
                return

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

    def walk(self) -> List[TreeNode]:
        root_key, views = self._start()
        if not root_key:
            return self.rows
        self._root_key = root_key
        self._visit(2, [], [], [])
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
        })
        return base
