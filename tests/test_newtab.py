"""The PyBrowser new-tab page: rendering, safety, actions and preferences.

The page is real HTML in the real engine, so most of this drives an actual
`BrowserTab` at `pybrowser://newtab/` and reads the DOM back. Only the parts
that would open a modal dialog or need a network are exercised at the signal
level instead.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_newtab -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-newtab-tests-"))
# The new-tab page must never touch the keyring; this makes a failure to keep
# that promise show up as a test failure rather than a crashed process.
os.environ.setdefault("PYBROWSER_DISABLE_KEYRING", "1")

import app.browser  # noqa: E402,F401 - registers pybrowser:// before QApplication

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.newtab import (  # noqa: E402
    ACTION_PREFIX,
    NEW_TAB_URL,
    NewTabData,
    collect,
    is_new_tab,
    parse_action,
    render,
)
from tests.qt_profile import shared_profile  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from app.storage.settings import (  # noqa: E402
    NEW_TAB_BLANK,
    NEW_TAB_CUSTOM,
    NEW_TAB_PYBROWSER,
    NEW_TAB_SEARCH,
    SettingsStore,
)

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def tearDownModule() -> None:
    if _app is not None:
        for _ in range(3):
            _app.processEvents()
    # The profile is shared across the whole test process and outlives this
    # module; see tests/qt_profile.py.


def pump(predicate, timeout_ms: int = 15000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class _Entry:
    def __init__(self, url: str, title: str) -> None:
        self.url, self.title = url, title


class _History:
    def __init__(self, entries) -> None:
        self._entries = entries

    def recent(self, limit=200):
        return self._entries[:limit]


class _Bookmarks:
    def __init__(self, entries) -> None:
        self._entries = entries

    def all(self):
        return self._entries


class _MemorySettings(SettingsStore):
    """SettingsStore over a dict, so preferences need no database."""

    def __init__(self, values=None) -> None:  # noqa: D107 - deliberately no super()
        self._values = dict(values or {})

    def get(self, key, default=None):
        if key in self._values:
            return self._values[key]
        from app.storage.settings import _DEFAULTS

        return default if default is not None else _DEFAULTS.get(key, "")

    def set(self, key, value):
        self._values[key] = value


# ---------------------------------------------------------------------------


class UrlTests(unittest.TestCase):
    def test_recognises_its_own_pages(self) -> None:
        self.assertTrue(is_new_tab(NEW_TAB_URL))
        self.assertTrue(is_new_tab(QUrl("pybrowser://newtab/action/search?q=x")))
        self.assertFalse(is_new_tab("https://example.com/"))
        self.assertFalse(is_new_tab("pybrowser://other/"))

    def test_parses_actions(self) -> None:
        name, params = parse_action(QUrl(f"pybrowser://newtab{ACTION_PREFIX}search?q=two+words"))
        self.assertEqual(name, "search")
        self.assertEqual(params["q"], "two words")

    def test_the_page_itself_is_not_an_action(self) -> None:
        self.assertIsNone(parse_action(QUrl(NEW_TAB_URL)))
        self.assertIsNone(parse_action(QUrl("https://example.com/action/search")))

    def test_the_address_bar_shows_nothing_for_a_new_tab(self) -> None:
        from app.utils import urls as url_utils

        self.assertEqual(url_utils.display_text(QUrl(NEW_TAB_URL)), "")

    def test_internal_pages_are_never_recorded(self) -> None:
        from app.storage.bookmarks import BookmarkStore
        from app.storage.history import HistoryStore

        self.assertFalse(HistoryStore(None).should_record(NEW_TAB_URL))
        self.assertFalse(BookmarkStore(None).is_bookmarkable(NEW_TAB_URL))


class DataTests(unittest.TestCase):
    def test_collect_deduplicates_and_limits(self) -> None:
        entries = [_Entry("https://a.com", "A"), _Entry("https://a.com", "A again"),
                   _Entry("https://b.com", "B"), _Entry("https://c.com", "C")]
        data = collect(_History(entries), _Bookmarks([]), limit=2)
        self.assertEqual([row["url"] for row in data.recent], ["https://a.com", "https://b.com"])

    def test_collect_hides_the_new_tab_page_from_its_own_list(self) -> None:
        data = collect(_History([_Entry(NEW_TAB_URL, "New Tab"),
                                 _Entry("https://a.com", "A")]), None)
        self.assertEqual([row["url"] for row in data.recent], ["https://a.com"])

    def test_a_broken_store_still_yields_a_page(self) -> None:
        class Exploding:
            def recent(self, limit=200):
                raise RuntimeError("database is locked")

        data = collect(Exploding(), None)
        self.assertEqual(data.recent, [])
        # The wordmark is split as Py<span>Browser</span>, so check the markup.
        self.assertIn("Py<span>Browser</span>", render(data))

    def test_a_hostile_title_cannot_break_out_of_the_json(self) -> None:
        data = NewTabData(recent=[{"title": "</script><img src=x onerror=alert(1)>",
                                   "url": "https://evil.example/"}])
        html = render(data)
        self.assertNotIn("</script><img", html)
        self.assertIn("\\u003c/script", html)

    def test_the_payload_is_valid_json(self) -> None:
        data = NewTabData(recent=[{"title": "Ünïcøde ✓", "url": "https://a.com"}])
        # The "<" escaping must leave the document parseable as JSON.
        self.assertEqual(json.loads(data.to_json())["recent"][0]["title"], "Ünïcøde ✓")

    def test_the_page_loads_nothing_from_the_network(self) -> None:
        """Nothing is fetched, so the page appears instantly and works offline.

        Images are allowed - Py is one - but only inlined. This used to forbid
        `<img` outright, which was a proxy for the real rule; now it checks the
        rule itself, which is stricter: every image source must be a data URI.
        """
        import re

        html = render(NewTabData())
        for pattern in ("http://", "https://", "//cdn", "@import", "url("):
            self.assertNotIn(pattern, html, f"new tab page must not reference {pattern}")
        for source in re.findall(r"""<img[^>]*\ssrc=["']([^"']+)""", html):
            self.assertTrue(source.startswith("data:"),
                            f"image is fetched rather than inlined: {source[:40]}")

    def test_py_is_on_the_page(self) -> None:
        html = render(NewTabData())
        self.assertIn("data:image", html, "Py is missing from the new tab page")
        self.assertIn("Hey, I\u2019m Py", html)

    def test_the_offers_say_what_they_are_for(self) -> None:
        # "Compare" on its own is a word, not an offer.
        html = render(NewTabData())
        for label, blurb in (("Research", "Go deep on a topic"),
                             ("Summarise", "Get the key points"),
                             ("Compare", "Look across my tabs"),
                             ("Explain", "Make it simple and clear")):
            self.assertIn(label, html)
            self.assertIn(blurb, html)


class SettingsTests(unittest.TestCase):
    def test_pybrowser_new_tab_is_the_default(self) -> None:
        settings = _MemorySettings()
        self.assertEqual(settings.new_tab_mode, NEW_TAB_PYBROWSER)
        self.assertEqual(settings.new_tab_url(), NEW_TAB_URL)

    def test_search_mode_follows_the_configured_provider(self) -> None:
        settings = _MemorySettings({"new_tab_mode": NEW_TAB_SEARCH,
                                    "search_url": "https://example.org/find?q={query}"})
        self.assertEqual(settings.new_tab_url(), "https://example.org/")

    def test_custom_mode_uses_the_custom_address(self) -> None:
        settings = _MemorySettings({"new_tab_mode": NEW_TAB_CUSTOM,
                                    "new_tab_custom_url": "https://example.com/start"})
        self.assertEqual(settings.new_tab_url(), "https://example.com/start")

    def test_an_empty_custom_address_falls_back_rather_than_opening_nothing(self) -> None:
        settings = _MemorySettings({"new_tab_mode": NEW_TAB_CUSTOM})
        self.assertEqual(settings.new_tab_url(), NEW_TAB_URL)

    def test_blank_mode(self) -> None:
        self.assertEqual(_MemorySettings({"new_tab_mode": NEW_TAB_BLANK}).new_tab_url(),
                         "about:blank")

    def test_an_unknown_stored_mode_falls_back(self) -> None:
        self.assertEqual(_MemorySettings({"new_tab_mode": "nonsense"}).new_tab_url(),
                         NEW_TAB_URL)


class RenderedPageTests(unittest.TestCase):
    """The real page, in the real engine."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.recent = [_Entry("https://example.com/one", "Example One")]
        cls.marks = [type("B", (), {"url": "https://book.example/", "title": "Bookmarked"})()]
        _profile.set_new_tab_provider(
            lambda: collect(_History(cls.recent), _Bookmarks(cls.marks), agent_available=True))

    def setUp(self) -> None:
        self.tabs = TabManager(_profile, NEW_TAB_URL)
        self.tabs.resize(1100, 800)
        self.tabs.show()
        self.tab = self.tabs.new_tab(NEW_TAB_URL)
        finished = []
        self.tab.load_finished.connect(finished.append)
        self.assertTrue(pump(lambda: finished), "the new tab page did not load")
        self.assertTrue(finished[0], "the new tab page reported a failed load")

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def js(self, script: str, timeout_ms: int = 8000):
        out = []
        self.tab.run_javascript(script, out.append)
        self.assertTrue(pump(lambda: out, timeout_ms), f"no result from: {script}")
        return out[0]

    def test_it_is_served_from_the_custom_scheme(self) -> None:
        self.assertEqual(self.tab.url().toString(), NEW_TAB_URL)

    def test_it_shows_pybrowser_branding(self) -> None:
        self.assertEqual(self.js("document.querySelector('.wordmark').textContent"),
                         "PyBrowser")

    def test_the_tab_is_labelled_new_tab(self) -> None:
        self.assertEqual(self.tabs.tabText(self.tabs.indexOf(self.tab)), "New Tab")

    def test_it_lists_recent_pages_and_bookmarks(self) -> None:
        self.assertEqual(self.js("document.querySelector('#recent a').textContent"),
                         "Example Oneexample.com")
        self.assertEqual(self.js("document.querySelector('#bookmarks a').textContent"),
                         "Bookmarkedbook.example")

    def test_a_hostile_title_renders_as_text_not_markup(self) -> None:
        _profile.set_new_tab_provider(lambda: collect(
            _History([_Entry("https://evil.example/", "<img src=x onerror=window.pwned=1>")]),
            None))
        try:
            finished = []
            self.tab.load_finished.connect(finished.append)
            self.tab.reload()
            self.assertTrue(pump(lambda: finished))
            self.assertEqual(self.js("document.querySelectorAll('#recent img').length"), 0)
            self.assertEqual(self.js("String(window.pwned)"), "undefined")
            self.assertIn("<img", self.js("document.querySelector('#recent a').textContent"))
        finally:
            _profile.set_new_tab_provider(lambda: collect(
                _History(self.recent), _Bookmarks(self.marks), agent_available=True))

    def test_the_search_box_asks_for_focus_so_you_can_just_type(self) -> None:
        """Assert the markup contract, not the live focus.

        Whether the box *actually* holds focus depends on the window being the
        active one, which is not reliably true for an offscreen test window -
        asserting `document.activeElement` here failed on roughly one run in
        three for reasons that had nothing to do with the page.
        """
        self.assertTrue(self.js("document.getElementById('q').hasAttribute('autofocus')"))

    def test_submitting_the_box_asks_the_browser_to_search(self) -> None:
        actions = []
        self.tabs.internal_action.connect(lambda name, params: actions.append((name, dict(params))))
        self.tab.run_javascript(
            "document.getElementById('q').value='best gaming laptops';"
            "document.getElementById('f').requestSubmit();")
        self.assertTrue(pump(lambda: actions, 6000), "no action was emitted")
        self.assertEqual(actions[0], ("search", {"q": "best gaming laptops"}))

    def test_the_page_is_still_on_screen_after_asking(self) -> None:
        # The action is intercepted and refused, so the page must not have
        # navigated itself anywhere.
        actions = []
        self.tabs.internal_action.connect(lambda name, params: actions.append(name))
        self.tab.run_javascript("document.getElementById('ai').click();")
        self.assertTrue(pump(lambda: actions, 6000))
        self.assertEqual(actions[0], "ai")
        self.assertEqual(self.tab.url().toString(), NEW_TAB_URL)

    def test_clicking_a_recent_page_asks_the_browser_to_open_it(self) -> None:
        actions = []
        self.tabs.internal_action.connect(lambda name, params: actions.append((name, dict(params))))
        self.tab.run_javascript("document.querySelector('#recent a').click();")
        self.assertTrue(pump(lambda: actions, 6000))
        self.assertEqual(actions[0], ("open", {"url": "https://example.com/one"}))

    def test_the_hint_distinguishes_an_address_from_a_search(self) -> None:
        def hint(text: str) -> str:
            return self.js(
                f"(function(){{var b=document.getElementById('q');b.value={text!r};"
                "b.dispatchEvent(new Event('input'));"
                "return document.getElementById('hint').textContent;})()")

        self.assertIn("address", hint("youtube.com"))
        self.assertIn("search", hint("best gaming laptops"))

    def test_empty_states_appear_with_no_data(self) -> None:
        _profile.set_new_tab_provider(NewTabData)
        try:
            finished = []
            self.tab.load_finished.connect(finished.append)
            self.tab.reload()
            self.assertTrue(pump(lambda: finished))
            self.assertEqual(self.js("document.querySelectorAll('.empty').length"), 2)
        finally:
            _profile.set_new_tab_provider(lambda: collect(
                _History(self.recent), _Bookmarks(self.marks), agent_available=True))


if __name__ == "__main__":
    unittest.main()
