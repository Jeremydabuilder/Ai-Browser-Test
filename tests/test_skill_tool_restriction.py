"""ToolRegistry's allowed_tools allowlist - the actual enforcement point
for a Skill's tool scope (app/agent/skills.py). No new execution path:
same ToolRegistry, same knows()/schemas()/run(), just optionally narrowed.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_skill_tool_restriction -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-skill-tools-tests-"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.tools import ToolError, ToolRegistry  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


class AllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(800, 600)
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def test_no_allowlist_behaves_exactly_as_before(self) -> None:
        registry = ToolRegistry(self.browser)
        self.assertTrue(registry.knows("browser_get_page_text"))
        self.assertTrue(registry.knows("browser_click"))
        schemas = {s["name"] for s in registry.schemas()}
        self.assertIn("browser_click", schemas)

    def test_an_allowed_tool_is_known_and_offered(self) -> None:
        registry = ToolRegistry(self.browser, allowed_tools=frozenset({"browser_get_page_text"}))
        self.assertTrue(registry.knows("browser_get_page_text"))
        schemas = {s["name"] for s in registry.schemas()}
        self.assertEqual(schemas, {"browser_get_page_text"})

    def test_a_tool_outside_the_allowlist_is_unknown(self) -> None:
        registry = ToolRegistry(self.browser, allowed_tools=frozenset({"browser_get_page_text"}))
        self.assertFalse(registry.knows("browser_click"))
        self.assertFalse(registry.knows("browser_navigate"))

    def test_a_disallowed_tool_is_never_in_the_schema_list_sent_to_the_model(self) -> None:
        registry = ToolRegistry(self.browser, allowed_tools=frozenset({"browser_get_page_text"}))
        names = {s["name"] for s in registry.schemas()}
        self.assertNotIn("browser_click", names)
        self.assertNotIn("browser_navigate", names)

    def test_run_refuses_a_disallowed_tool_even_called_directly(self) -> None:
        """Defense in depth: run() enforces the allowlist itself, not only
        knows() - a caller that skips straight to run() is still refused."""
        registry = ToolRegistry(self.browser, allowed_tools=frozenset({"browser_get_page_text"}))
        with self.assertRaises(ToolError):
            registry.run("browser_navigate", {"url": "https://example.com/"})

    def test_an_allowed_tool_still_runs_normally(self) -> None:
        registry = ToolRegistry(self.browser, allowed_tools=frozenset({"browser_get_page_text"}))
        outcome = registry.run("browser_get_page_text", {})
        self.assertIsNotNone(outcome.future)
        outcome.future.wait()

    def test_an_empty_allowlist_permits_nothing(self) -> None:
        registry = ToolRegistry(self.browser, allowed_tools=frozenset())
        self.assertFalse(registry.knows("browser_get_page_text"))
        self.assertEqual(registry.schemas(), [])

    def test_mission_tools_can_be_allowed_alongside_browser_tools(self) -> None:
        registry = ToolRegistry(self.browser, allowed_tools=frozenset(
            {"browser_get_page_text", "mission_save_finding"}))
        self.assertTrue(registry.knows("mission_save_finding"))
        self.assertFalse(registry.knows("mission_save_decision"))

    def test_an_mcp_tool_name_can_be_allowed_specifically(self) -> None:
        """A Skill scoped to one MCP tool on one server - the namespaced
        name already encodes both, so no extra scoping mechanism is needed."""
        class _FakeMcp:
            def knows(self, name: str) -> bool:
                return name == "mcp.server1.search"

            def schemas(self):
                return [{"name": "mcp.server1.search", "description": "", "input_schema": {}},
                       {"name": "mcp.server1.delete_everything", "description": "",
                        "input_schema": {}}]

        registry = ToolRegistry(self.browser, mcp=_FakeMcp(),
                                allowed_tools=frozenset({"mcp.server1.search"}))
        self.assertTrue(registry.knows("mcp.server1.search"))
        self.assertFalse(registry.knows("mcp.server1.delete_everything"))
        names = {s["name"] for s in registry.schemas()}
        self.assertEqual(names, {"mcp.server1.search"})


if __name__ == "__main__":
    unittest.main()
