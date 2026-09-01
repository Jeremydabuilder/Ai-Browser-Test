"""End-to-end tests for the BrowserController automation API.

These drive a real Qt WebEngine browser against the deterministic fixture
server in ``tests/fixture_server.py``. No external website is involved, so the
suite is reproducible offline and unaffected by any network policy.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_browser_controller -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-ctl-tests-"))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.controller import BrowserController, ScrollDirection  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402
from app.browser.results import ErrorCode  # noqa: E402
from app.browser.safety import Sensitivity  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402

_app: QApplication | None = None
_server: FixtureServer | None = None
_profile = None


def setUpModule() -> None:
    global _app, _server, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _server = FixtureServer()
    _profile = shared_profile()


def tearDownModule() -> None:
    if _server is not None:
        _server.stop()
    # Drain pending deleteLater calls before the shared profile goes away.
    # Qt still prints "Release of profile requested but WebEnginePage still not
    # deleted" at interpreter exit; that is Python's GC ordering at shutdown,
    # after this runs, and it is cosmetic - the tests have already finished.
    if _app is not None:
        for _ in range(3):
            _app.processEvents()
    # The profile is shared across the whole test process and outlives this
    # module; see tests/qt_profile.py.
    if _app is not None:
        _app.processEvents()


def pump(predicate, timeout_ms: int = 10000) -> bool:
    """Spin the event loop until ``predicate`` holds or time runs out."""
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


def sleep_ms(milliseconds: int) -> None:
    """Let the event loop run for a fixed period (for genuinely timed waits)."""
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


class ControllerTestCase(unittest.TestCase):
    """Each test gets a fresh TabManager and controller on the shared profile."""

    def setUp(self) -> None:
        self.server = _server
        self.tabs = TabManager(_profile, self.server.base)
        # The tab strip needs a real size. In a zero-width widget every
        # block-level element measures 0 wide and is correctly judged
        # invisible, and there is nothing to scroll - which would make these
        # tests measure the harness rather than the browser.
        self.tabs.resize(1200, 800)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self.tab_id = self.browser.open_tab().wait().effects.new_tab_id

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        # Let the deleteLater calls run before the shared profile outlives its
        # pages, which Qt warns about loudly at interpreter shutdown.
        _app.processEvents()

    # -- helpers ---------------------------------------------------------
    def open_home(self):
        result = self.browser.navigate(self.server.base).wait()
        self.assertTrue(result.ok, result.error)
        return self.structure()

    def structure(self, **kwargs):
        result = self.browser.get_page_structure(**kwargs).wait()
        self.assertTrue(result.ok, result.error)
        return result.data["structure"]

    def button(self, structure, name: str):
        element = next((e for e in structure.buttons if e.name.startswith(name)), None)
        self.assertIsNotNone(element, f"no button named {name!r}")
        return element

    def link(self, structure, name: str):
        element = next((e for e in structure.links if e.name.startswith(name)), None)
        self.assertIsNotNone(element, f"no link named {name!r}")
        return element


# ---------------------------------------------------------------------------
class PageInspectionTests(ControllerTestCase):
    def test_structure_reports_page_identity(self):
        structure = self.open_home()
        self.assertEqual(structure.title, "Fixture Home")
        self.assertTrue(structure.url.startswith(self.server.base))
        self.assertEqual(structure.lang, "en")
        self.assertTrue(structure.snapshot_id.startswith("s"))

    def test_headings_are_captured_with_levels(self):
        structure = self.open_home()
        self.assertEqual(
            [(h.level, h.text) for h in structure.headings][:2],
            [(1, "Fixture Home"), (2, "Controls")],
        )

    def test_links_buttons_and_fields_are_categorised(self):
        structure = self.open_home()
        self.assertTrue(any(e.name == "Second page" for e in structure.links))
        self.assertTrue(any(e.name == "Add a button" for e in structure.buttons))
        self.assertTrue(any(e.role == "searchbox" for e in structure.text_fields))
        self.assertTrue(any(e.role == "textarea" for e in structure.text_fields))
        self.assertEqual(len(structure.checkboxes), 1)
        self.assertEqual(len(structure.radios), 2)
        self.assertEqual(len(structure.selects), 1)

    def test_accessible_names_come_from_labels(self):
        structure = self.open_home()
        search = next(e for e in structure.text_fields if e.role == "searchbox")
        self.assertEqual(search.name, "Search terms")
        self.assertEqual(search.placeholder, "Search the fixtures")

    def test_select_reports_its_options_and_value(self):
        structure = self.open_home()
        select = structure.selects[0]
        self.assertEqual(select.value, "green")
        self.assertEqual([o["label"] for o in select.options], ["Red", "Green", "Blue"])

    def test_forms_are_reported_and_fields_link_back_to_them(self):
        structure = self.open_home()
        self.assertEqual(len(structure.forms), 1)
        form = structure.forms[0]
        self.assertEqual(form.method, "get")
        self.assertTrue(form.action.endswith("/results"))
        search = next(e for e in structure.text_fields if e.role == "searchbox")
        self.assertEqual(search.form, 0)

    def test_disabled_elements_are_flagged_not_hidden(self):
        structure = self.open_home()
        disabled = [e for e in structure.elements if e.disabled]
        self.assertEqual([e.name for e in disabled], ["Disabled button"])

    def test_invisible_elements_are_excluded_by_default(self):
        structure = self.open_home()
        self.assertFalse(any("Hidden button" in e.name for e in structure.elements))

    def test_invisible_elements_can_be_requested_explicitly(self):
        self.browser.navigate(self.server.base).wait()
        structure = self.structure(include_invisible=True)
        hidden = [e for e in structure.elements if e.name == "Hidden button"]
        self.assertEqual(len(hidden), 1)
        self.assertFalse(hidden[0].visible)

    def test_password_values_are_never_reported(self):
        structure = self.open_home()
        password = next(e for e in structure.elements if e.input_type == "password")
        self.browser.type_text(password.ref, "hunter2").wait()
        refreshed = self.structure()
        field = next(e for e in refreshed.elements if e.input_type == "password")
        self.assertTrue(field.secret)
        self.assertNotIn("hunter2", field.value or "")

    def test_raw_html_is_not_part_of_the_representation(self):
        structure = self.open_home()
        serialised = structure.to_json()
        self.assertNotIn("<button", serialised)
        self.assertNotIn("<!doctype", serialised.lower())

    def test_element_limit_is_honoured_and_reported(self):
        self.browser.navigate(self.server.base).wait()
        structure = self.structure(max_elements=3)
        self.assertEqual(structure.element_count, 3)
        self.assertTrue(structure.elements_truncated)

    def test_text_limit_is_honoured_and_reported(self):
        self.browser.navigate(self.server.base).wait()
        structure = self.structure(max_text=40)
        self.assertLessEqual(len(structure.text), 40)
        self.assertTrue(structure.text_truncated)

    def test_get_page_text_returns_readable_text(self):
        self.browser.navigate(self.server.base).wait()
        result = self.browser.get_page_text().wait()
        self.assertTrue(result.ok)
        self.assertIn("Fixture Home", result.data["text"])


# ---------------------------------------------------------------------------
class ElementReferenceTests(ControllerTestCase):
    def test_references_are_scoped_to_their_snapshot(self):
        first = self.open_home()
        second = self.structure()
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertTrue(first.elements[0].ref.startswith(first.snapshot_id + ":"))
        self.assertTrue(second.elements[0].ref.startswith(second.snapshot_id + ":"))

    def test_old_snapshot_still_works_while_the_page_is_unchanged(self):
        """Refs are scoped, not single-use: an untouched page stays addressable."""
        first = self.open_home()
        self.structure()  # a newer snapshot exists
        target = self.button(first, "Clicked 0 times")
        result = self.browser.click(target.ref).wait()
        self.assertTrue(result.ok, result.error)

    def test_reference_from_before_a_navigation_is_rejected(self):
        structure = self.open_home()
        target = self.button(structure, "Clicked 0 times")
        self.browser.navigate(self.server.url("second")).wait()
        result = self.browser.click(target.ref).wait()
        self.assertFalse(result.ok)
        self.assertIn(result.error.code, (ErrorCode.STALE_SNAPSHOT, ErrorCode.STALE_DOCUMENT))
        self.assertTrue(result.error.recoverable)
        self.assertTrue(result.should_reinspect)

    def test_reference_to_a_removed_element_is_rejected(self):
        structure = self.open_home()
        victim = self.button(structure, "Removable target")
        remover = self.button(structure, "Remove the target")
        self.browser.click(remover.ref).wait()
        result = self.browser.click(victim.ref).wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.STALE_DETACHED)
        self.assertTrue(result.error.recoverable)

    def test_reference_to_a_recycled_element_is_rejected(self):
        """The important one: a node reused for different content must not be clicked.

        The fixture rewrites the label of an existing button. Nothing was
        removed, so a naive implementation would happily click it - and the
        caller would be acting on something it never saw.
        """
        structure = self.open_home()
        victim = self.button(structure, "Removable target")
        recycler = self.button(structure, "Recycle label")
        self.browser.click(recycler.ref).wait()
        result = self.browser.click(victim.ref).wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.STALE_MUTATED)
        self.assertTrue(result.error.recoverable)
        self.assertIn("different content", result.error.message)

    def test_recovering_from_a_stale_reference_works(self):
        structure = self.open_home()
        victim = self.button(structure, "Removable target")
        self.browser.click(self.button(structure, "Recycle label").ref).wait()
        stale = self.browser.click(victim.ref).wait()
        self.assertTrue(stale.should_reinspect)
        # The documented recovery: inspect again, act on a fresh reference.
        fresh = self.structure()
        renamed = self.button(fresh, "Completely different action")
        self.assertTrue(self.browser.click(renamed.ref).wait().ok)

    def test_unknown_reference_in_a_valid_snapshot(self):
        structure = self.open_home()
        result = self.browser.click(f"{structure.snapshot_id}:e999").wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.UNKNOWN_REF)

    def test_unknown_snapshot(self):
        self.open_home()
        result = self.browser.click("s999:e0").wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.STALE_SNAPSHOT)

    def test_malformed_references_are_rejected_without_touching_the_page(self):
        self.open_home()
        for bad in ("", "e0", "nonsense", "s1-e0", "s1:x0", "'; alert(1); //"):
            result = self.browser.click(bad).wait()
            self.assertFalse(result.ok, bad)
            self.assertEqual(result.error.code, ErrorCode.INVALID_REF, bad)
            self.assertFalse(result.error.recoverable, bad)

    def test_inspect_element_confirms_a_reference_is_still_valid(self):
        structure = self.open_home()
        target = self.button(structure, "Clicked 0 times")
        good = self.browser.inspect_element(target.ref).wait()
        self.assertTrue(good.ok)
        self.assertEqual(good.data["element"]["name"], "Clicked 0 times")
        self.browser.click(self.button(structure, "Remove the target").ref).wait()
        victim = self.button(structure, "Removable target")
        self.assertFalse(self.browser.inspect_element(victim.ref).wait().ok)


# ---------------------------------------------------------------------------
class ActionTests(ControllerTestCase):
    def test_click_that_changes_the_dom_reports_dom_changed(self):
        structure = self.open_home()
        target = self.button(structure, "Clicked 0 times")
        result = self.browser.click(target.ref).wait()
        self.assertTrue(result.ok)
        self.assertTrue(result.effects.dom_changed)
        self.assertFalse(result.effects.navigated)
        self.assertEqual(result.target.name, "Clicked 0 times")
        refreshed = self.structure()
        self.assertTrue(any(e.name == "Clicked 1 times" for e in refreshed.buttons))

    def test_click_that_navigates_reports_navigation(self):
        structure = self.open_home()
        result = self.browser.click(self.link(structure, "Second page").ref).wait()
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.effects.navigated)
        self.assertTrue(result.effects.url_after.endswith("/second"))
        self.assertEqual(result.page.title, "Second Page")

    def test_javascript_initiated_navigation_is_detected(self):
        structure = self.open_home()
        result = self.browser.click(self.button(structure, "Go via JavaScript").ref).wait()
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.effects.navigated)
        self.assertTrue(result.page.url.endswith("/second"))

    def test_click_on_a_disabled_element_is_refused(self):
        structure = self.open_home()
        result = self.browser.click(self.button(structure, "Disabled button").ref).wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.ELEMENT_DISABLED)
        self.assertFalse(result.error.recoverable)

    def test_click_on_an_invisible_element_is_refused(self):
        self.browser.navigate(self.server.base).wait()
        structure = self.structure(include_invisible=True)
        hidden = next(e for e in structure.elements if e.name == "Hidden button")
        result = self.browser.click(hidden.ref).wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.ELEMENT_NOT_VISIBLE)

    def test_element_becomes_clickable_once_revealed(self):
        self.browser.navigate(self.server.base).wait()
        structure = self.structure()
        self.browser.click(self.button(structure, "Reveal hidden").ref).wait()
        refreshed = self.structure()
        revealed = next(e for e in refreshed.elements if e.name == "Hidden button")
        self.assertTrue(revealed.visible)
        self.assertTrue(self.browser.click(revealed.ref).wait().ok)

    def test_typing_sets_a_value(self):
        structure = self.open_home()
        search = next(e for e in structure.text_fields if e.role == "searchbox")
        result = self.browser.type_text(search.ref, "hello fixtures").wait()
        self.assertTrue(result.ok, result.error)
        refreshed = self.structure()
        field = next(e for e in refreshed.text_fields if e.role == "searchbox")
        self.assertEqual(field.value, "hello fixtures")

    def test_typing_appends_when_asked(self):
        structure = self.open_home()
        search = next(e for e in structure.text_fields if e.role == "searchbox")
        self.browser.type_text(search.ref, "abc").wait()
        self.browser.type_text(search.ref, "def", append=True).wait()
        refreshed = self.structure()
        self.assertEqual(
            next(e for e in refreshed.text_fields if e.role == "searchbox").value, "abcdef")

    def test_typing_into_a_non_editable_element_is_refused(self):
        structure = self.open_home()
        button = self.button(structure, "Clicked 0 times")
        result = self.browser.type_text(button.ref, "nope").wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.ELEMENT_NOT_EDITABLE)

    def test_typing_into_a_textarea(self):
        structure = self.open_home()
        notes = next(e for e in structure.text_fields if e.role == "textarea")
        self.assertTrue(self.browser.type_text(notes.ref, "some notes").wait().ok)
        refreshed = self.structure()
        self.assertEqual(
            next(e for e in refreshed.text_fields if e.role == "textarea").value, "some notes")

    def test_checkbox_can_be_checked_and_unchecked(self):
        structure = self.open_home()
        checkbox = structure.checkboxes[0]
        self.assertTrue(self.browser.set_checked(checkbox.ref, True).wait().ok)
        self.assertTrue(self.structure().checkboxes[0].checked)
        self.assertTrue(self.browser.set_checked(checkbox.ref, False).wait().ok)
        self.assertFalse(self.structure().checkboxes[0].checked)

    def test_set_checked_on_a_non_checkbox_is_refused(self):
        structure = self.open_home()
        result = self.browser.set_checked(self.button(structure, "Clicked 0 times").ref).wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.ELEMENT_NOT_CHECKABLE)

    def test_radio_selection(self):
        structure = self.open_home()
        large = next(e for e in structure.radios if e.name == "Large")
        self.assertTrue(self.browser.set_checked(large.ref, True).wait().ok)
        refreshed = self.structure()
        self.assertTrue(next(e for e in refreshed.radios if e.name == "Large").checked)

    def test_select_option_by_label_and_by_value(self):
        structure = self.open_home()
        select = structure.selects[0]
        self.assertTrue(self.browser.select_option(select.ref, "Blue").wait().ok)
        self.assertEqual(self.structure().selects[0].value, "blue")
        self.assertTrue(self.browser.select_option(select.ref, "red").wait().ok)
        self.assertEqual(self.structure().selects[0].value, "red")

    def test_select_option_that_does_not_exist_is_refused(self):
        structure = self.open_home()
        result = self.browser.select_option(structure.selects[0].ref, "Chartreuse").wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.OPTION_NOT_FOUND)

    def test_form_submission_navigates_with_the_typed_value(self):
        structure = self.open_home()
        search = next(e for e in structure.text_fields if e.role == "searchbox")
        result = self.browser.type_text(search.ref, "kittens", submit=True).wait()
        self.assertTrue(result.ok, result.error)
        self.assertTrue(pump(lambda: "results" in self.browser.get_current_page().page.url, 10000))
        text = self.browser.get_page_text().wait().data["text"]
        self.assertIn("query=kittens", text)

    def test_submit_on_an_element_outside_a_form_is_refused(self):
        structure = self.open_home()
        result = self.browser.submit(self.button(structure, "Clicked 0 times").ref).wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.NO_FORM)

    def test_scrolling_moves_and_reports_position(self):
        self.open_home()
        result = self.browser.scroll(ScrollDirection.DOWN).wait()
        self.assertTrue(result.ok)
        self.assertGreater(result.effects.scroll_after, result.effects.scroll_before)
        bottom = self.browser.scroll(ScrollDirection.BOTTOM).wait()
        self.assertTrue(self.structure().at_bottom)
        top = self.browser.scroll(ScrollDirection.TOP).wait()
        self.assertEqual(top.effects.scroll_after, 0)
        self.assertGreater(bottom.effects.scroll_after, 0)

    def test_scroll_to_element_brings_it_into_view(self):
        structure = self.open_home()
        self.browser.scroll(ScrollDirection.BOTTOM).wait()
        target = self.button(structure, "Clicked 0 times")
        self.assertTrue(self.browser.scroll_to_element(target.ref).wait().ok)
        refreshed = self.structure()
        moved = next(e for e in refreshed.buttons if e.name.startswith("Clicked"))
        self.assertTrue(moved.in_viewport)


# ---------------------------------------------------------------------------
class DynamicContentTests(ControllerTestCase):
    def test_dynamically_generated_elements_appear_in_a_new_snapshot(self):
        structure = self.open_home()
        self.assertFalse(any("Generated" in e.name for e in structure.elements))
        self.browser.click(self.button(structure, "Add a button").ref).wait()
        refreshed = self.structure()
        self.assertTrue(any(e.name == "Generated button 1" for e in refreshed.buttons))
        self.assertTrue(any(e.name == "Generated link" for e in refreshed.links))
        self.assertTrue(any(e.placeholder == "Generated input" for e in refreshed.elements))

    def test_a_generated_element_is_usable(self):
        structure = self.open_home()
        self.browser.click(self.button(structure, "Add a button").ref).wait()
        refreshed = self.structure()
        generated = next(e for e in refreshed.elements if e.placeholder == "Generated input")
        self.assertTrue(self.browser.type_text(generated.ref, "typed").wait().ok)

    def test_wait_for_element_finds_delayed_content(self):
        """The fixture adds this element 700ms after load - long after loadFinished."""
        self.browser.navigate(self.server.base).wait()
        immediate = self.structure()
        self.assertFalse(any(e.name == "Delayed button" for e in immediate.buttons))
        result = self.browser.wait_for_element(role="button", name_contains="Delayed",
                                               timeout_ms=8000).wait()
        self.assertTrue(result.ok, result.error)
        self.assertGreaterEqual(result.data["matches"], 1)
        self.assertTrue(any(e.name == "Delayed button" for e in self.structure().buttons))

    def test_wait_for_element_can_match_page_text(self):
        self.browser.navigate(self.server.base).wait()
        result = self.browser.wait_for_element(text_contains="Delayed content has arrived",
                                               timeout_ms=8000).wait()
        self.assertTrue(result.ok, result.error)

    def test_wait_for_element_times_out_cleanly(self):
        self.open_home()
        result = self.browser.wait_for_element(role="button", name_contains="never exists",
                                               timeout_ms=700).wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.TIMEOUT)

    def test_click_with_no_observable_effect_reports_nothing_changed(self):
        structure = self.open_home()
        # Clicking a plain link's *container* is not available, so use a click
        # on an element whose handler does nothing: the search field.
        search = next(e for e in structure.text_fields if e.role == "searchbox")
        result = self.browser.click(search.ref).wait()
        self.assertTrue(result.ok)
        self.assertFalse(result.effects.navigated)


# ---------------------------------------------------------------------------
class NavigationTests(ControllerTestCase):
    def test_navigate_resolves_after_the_load_completes(self):
        result = self.browser.navigate(self.server.url("second")).wait()
        self.assertTrue(result.ok)
        self.assertFalse(result.page.loading)
        self.assertEqual(result.page.title, "Second Page")

    def test_navigate_reports_an_invalid_url(self):
        result = self.browser.navigate("http://").wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.INVALID_URL)

    def test_navigate_reports_a_failed_load(self):
        result = self.browser.navigate("http://127.0.0.1:47999/").wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.LOAD_FAILED)
        self.assertNotIn("ERR_", result.error.message)

    def test_redirects_are_followed_and_the_final_url_reported(self):
        result = self.browser.navigate(self.server.url("redirect")).wait()
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.page.url.endswith("/redirected"))
        self.assertEqual(result.page.title, "Redirect Target")

    def test_clicking_a_redirecting_link_ends_at_the_target(self):
        structure = self.open_home()
        result = self.browser.click(self.link(structure, "Redirecting link").ref).wait()
        self.assertTrue(result.ok, result.error)
        self.assertTrue(pump(lambda: self.browser.get_current_page().page.url.endswith("/redirected")))

    def test_slow_page_still_resolves(self):
        result = self.browser.navigate(self.server.url("slow")).wait(timeout_ms=20000)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.page.title, "Slow Page")

    def test_back_and_forward(self):
        self.browser.navigate(self.server.base).wait()
        self.browser.navigate(self.server.url("second")).wait()
        back = self.browser.go_back().wait()
        self.assertTrue(back.ok, back.error)
        self.assertTrue(pump(lambda: not self.browser.get_current_page().page.url.endswith("/second")))
        forward = self.browser.go_forward().wait()
        self.assertTrue(forward.ok, forward.error)
        self.assertTrue(pump(lambda: self.browser.get_current_page().page.url.endswith("/second")))

    def test_back_without_history_is_a_clean_refusal(self):
        result = self.browser.go_back().wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.NO_HISTORY)
        self.assertIn("no page to go back to", result.error.message)

    def test_reload_keeps_the_url_and_invalidates_references(self):
        structure = self.open_home()
        target = self.button(structure, "Clicked 0 times")
        result = self.browser.reload().wait()
        self.assertTrue(result.ok, result.error)
        after = self.browser.click(target.ref).wait()
        self.assertFalse(after.ok)
        self.assertTrue(after.should_reinspect)

    def test_get_current_page_reports_history_availability(self):
        self.browser.navigate(self.server.base).wait()
        self.assertFalse(self.browser.get_current_page().page.can_go_back)
        self.browser.navigate(self.server.url("second")).wait()
        self.assertTrue(self.browser.get_current_page().page.can_go_back)


# ---------------------------------------------------------------------------
class TabTests(ControllerTestCase):
    def test_open_and_close_tabs(self):
        before = self.browser.tab_count()
        opened = self.browser.open_tab(self.server.url("second")).wait()
        self.assertTrue(opened.ok, opened.error)
        self.assertTrue(opened.effects.opened_tab)
        self.assertEqual(self.browser.tab_count(), before + 1)
        new_id = opened.effects.new_tab_id
        closed = self.browser.close_tab(new_id)
        self.assertTrue(closed.ok)
        self.assertEqual(self.browser.tab_count(), before)

    def test_tabs_are_addressed_by_stable_ids_not_indexes(self):
        first = self.browser.open_tab(self.server.base).wait().effects.new_tab_id
        second = self.browser.open_tab(self.server.url("second")).wait().effects.new_tab_id
        self.assertNotEqual(first, second)
        self.browser.close_tab(first)
        # `second` kept its identity even though its index shifted.
        listed = {t["tab_id"] for t in self.browser.list_tabs()}
        self.assertIn(second, listed)
        self.assertTrue(self.browser.select_tab(second).ok)

    def test_closed_tab_id_is_never_reused(self):
        tab_id = self.browser.open_tab(self.server.base).wait().effects.new_tab_id
        self.browser.close_tab(tab_id)
        result = self.browser.close_tab(tab_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.UNKNOWN_TAB)

    def test_actions_can_target_a_specific_tab(self):
        home_tab = self.browser.open_tab(self.server.base).wait().effects.new_tab_id
        other_tab = self.browser.open_tab(self.server.url("second")).wait().effects.new_tab_id
        page = self.browser.get_current_page(home_tab).page
        self.assertFalse(page.url.endswith("/second"))
        self.assertTrue(self.browser.get_current_page(other_tab).page.url.endswith("/second"))

    def test_switching_tabs_changes_the_default_target(self):
        home_tab = self.browser.open_tab(self.server.base).wait().effects.new_tab_id
        other_tab = self.browser.open_tab(self.server.url("second")).wait().effects.new_tab_id
        self.browser.select_tab(home_tab)
        self.assertFalse(self.browser.get_current_page().page.url.endswith("/second"))
        self.browser.select_tab(other_tab)
        self.assertTrue(self.browser.get_current_page().page.url.endswith("/second"))

    def test_target_blank_link_opens_a_tab_and_is_reported(self):
        structure = self.open_home()
        before = self.browser.tab_count()
        result = self.browser.click(self.link(structure, "Open in new tab").ref).wait()
        self.assertTrue(result.ok, result.error)
        self.assertTrue(pump(lambda: self.browser.tab_count() > before, 8000))
        self.assertTrue(result.effects.opened_tab)
        self.assertIsNotNone(result.effects.new_tab_id)

    def test_list_tabs_reports_identity_and_activity(self):
        self.browser.open_tab(self.server.base).wait()
        listing = self.browser.list_tabs()
        self.assertTrue(all("tab_id" in t and "url" in t and "active" in t for t in listing))
        self.assertEqual(sum(1 for t in listing if t["active"]), 1)

    def test_unknown_tab_id_is_reported(self):
        result = self.browser.get_current_page(99999)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.NO_TAB)


# ---------------------------------------------------------------------------
class SensitivityTests(ControllerTestCase):
    """The classifier is advisory: it must inform, never block."""

    def test_ordinary_link_is_normal(self):
        structure = self.open_home()
        preview = self.browser.describe_action("click", self.link(structure, "Second page").ref)
        self.assertEqual(preview["level"], Sensitivity.NORMAL)
        self.assertFalse(preview["requires_confirmation"])

    def test_purchase_button_is_flagged_sensitive(self):
        structure = self.open_home()
        preview = self.browser.describe_action("click", self.button(structure, "Buy now").ref)
        self.assertEqual(preview["level"], Sensitivity.SENSITIVE)
        self.assertTrue(preview["requires_confirmation"])
        self.assertTrue(preview["reasons"])

    def test_download_link_is_flagged_sensitive(self):
        structure = self.open_home()
        preview = self.browser.describe_action("click", self.link(structure, "Download installer").ref)
        self.assertEqual(preview["level"], Sensitivity.SENSITIVE)

    def test_password_field_is_flagged_sensitive(self):
        structure = self.open_home()
        password = next(e for e in structure.elements if e.input_type == "password")
        preview = self.browser.describe_action("type_text", password.ref, text="secret")
        self.assertEqual(preview["level"], Sensitivity.SENSITIVE)

    def test_ordinary_typing_is_elevated_not_sensitive(self):
        structure = self.open_home()
        search = next(e for e in structure.text_fields if e.role == "searchbox")
        preview = self.browser.describe_action("type_text", search.ref, text="cats")
        self.assertEqual(preview["level"], Sensitivity.ELEVATED)
        self.assertFalse(preview["requires_confirmation"])

    def test_classification_does_not_block_the_action(self):
        """Sensitivity is advice. Nothing here enforces it - that is Phase 2's job."""
        structure = self.open_home()
        result = self.browser.click(self.button(structure, "Buy now").ref).wait()
        self.assertTrue(result.ok)
        self.assertEqual(result.sensitivity["level"], Sensitivity.SENSITIVE)

    def test_results_carry_the_assessment_for_auditing(self):
        structure = self.open_home()
        search = next(e for e in structure.text_fields if e.role == "searchbox")
        result = self.browser.type_text(search.ref, "cats").wait()
        self.assertIn("level", result.sensitivity)
        self.assertIn("requires_confirmation", result.sensitivity)


# ---------------------------------------------------------------------------
class ApiBoundaryTests(ControllerTestCase):
    """The controller must not leak Qt objects or accept caller-supplied script."""

    def test_no_public_method_accepts_or_runs_arbitrary_javascript(self):
        forbidden = ("execute_script", "run_javascript", "eval", "evaluate",
                     "run_js", "inject", "execute_js")
        for name in forbidden:
            self.assertFalse(hasattr(self.browser, name),
                             f"BrowserController must not expose {name}()")

    def test_results_are_plain_serialisable_data(self):
        import json as _json
        structure = self.open_home()
        result = self.browser.click(self.button(structure, "Clicked 0 times").ref).wait()
        encoded = _json.dumps(result.to_dict())
        self.assertIn('"action": "click"', encoded)
        self.assertIn('"ok": true', encoded)

    def test_structure_is_json_serialisable(self):
        import json as _json
        structure = self.open_home()
        decoded = _json.loads(structure.to_json())
        self.assertEqual(decoded["title"], "Fixture Home")
        self.assertTrue(decoded["elements"])

    def test_public_api_returns_no_qt_objects(self):
        from PySide6.QtCore import QObject
        structure = self.open_home()
        values = [
            self.browser.get_current_page(),
            self.browser.list_tabs(),
            self.browser.describe_action("click", structure.elements[0].ref),
            self.browser.close_tab(self.browser.open_tab().wait().effects.new_tab_id),
        ]
        for value in values:
            self.assertNotIsInstance(value, QObject)

    def test_automation_state_is_invisible_to_the_page(self):
        """The page must not be able to see or tamper with our helpers."""
        self.open_home()
        tab = self.browser._tab_for(None)
        seen: dict = {}
        tab.run_javascript("typeof window.__pb", lambda v: seen.__setitem__("v", v))
        pump(lambda: "v" in seen, 5000)
        self.assertEqual(seen.get("v"), "undefined")

    def test_no_marker_attributes_are_left_in_the_page(self):
        self.open_home()
        tab = self.browser._tab_for(None)
        seen: dict = {}
        tab.run_javascript(
            "document.querySelectorAll('[data-pybrowser-ref]').length",
            lambda v: seen.__setitem__("v", v))
        pump(lambda: "v" in seen, 5000)
        self.assertEqual(seen.get("v"), 0)


# ---------------------------------------------------------------------------
class AsyncModelTests(ControllerTestCase):
    def test_future_resolves_via_callback(self):
        received: list = []
        self.browser.navigate(self.server.base).then(received.append)
        self.assertTrue(pump(lambda: bool(received), 15000))
        self.assertTrue(received[0].ok)

    def test_future_resolves_via_signal(self):
        received: list = []
        future = self.browser.navigate(self.server.base)
        future.finished.connect(received.append)
        self.assertTrue(pump(lambda: bool(received), 15000))
        self.assertTrue(received[0].ok)

    def test_then_on_an_already_resolved_future_runs_immediately(self):
        future = self.browser.navigate(self.server.base)
        future.wait()
        received: list = []
        future.then(received.append)
        self.assertEqual(len(received), 1)

    def test_a_future_resolves_exactly_once(self):
        received: list = []
        future = self.browser.navigate(self.server.base)
        future.then(received.append)
        future.wait()
        sleep_ms(400)
        self.assertEqual(len(received), 1)

    def test_synchronous_failures_still_return_a_resolved_future(self):
        future = self.browser.navigate("http://")
        self.assertTrue(future.done)
        self.assertEqual(future.result().error.code, ErrorCode.INVALID_URL)

    def test_wait_for_load_is_immediate_when_idle(self):
        self.browser.navigate(self.server.base).wait()
        result = self.browser.wait_for_load().wait()
        self.assertTrue(result.ok)
        self.assertFalse(result.page.loading)

    def test_every_result_reports_a_duration(self):
        result = self.browser.navigate(self.server.base).wait()
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_action_completed_signal_fires_for_each_operation(self):
        seen: list = []
        self.browser.action_completed.connect(seen.append)
        self.browser.navigate(self.server.base).wait()
        self.browser.get_current_page()
        self.assertGreaterEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
