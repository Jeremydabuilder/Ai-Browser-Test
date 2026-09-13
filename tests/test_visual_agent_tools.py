"""Phase 14 - Visual Computer-Use Fallback: agent-tool-layer tests.

Same real ToolRegistry/BrowserController pairing test_skill_tool_restriction
already uses - no fakes for the browser side. Covers: provider-vision
gating of the visual tool schemas, structured-tools-preferred ordering,
sensitive-visual-action confirmation (fail closed), unknown visual op
handling, and the per-task budget wired into ToolRegistry.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_visual_agent_tools -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-visual-tools-tests-"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.tools import TOOL_NAMES, VISUAL_TOOL_NAMES, ToolError, ToolRegistry  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_server: FixtureServer | None = None
_profile = None

NORMAL_BUTTON = (110, 60)
BUY_BUTTON = (110, 160)
TEXT_FIELD = (115, 615)
PASSWORD_FIELD = (115, 665)


def setUpModule() -> None:
    global _app, _server, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _server = FixtureServer()
    _profile = shared_profile()


def tearDownModule() -> None:
    if _server is not None:
        _server.stop()
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


class ProviderGatingTests(unittest.TestCase):
    """browser_visual_* tools must never be offered to a text-only
    provider - the whole point of the tool is reading an image it cannot
    process (the phase's own provider-capability requirement)."""

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

    def test_visual_tools_are_hidden_by_default(self) -> None:
        registry = ToolRegistry(self.browser)
        names = {s["name"] for s in registry.schemas()}
        self.assertFalse(names & VISUAL_TOOL_NAMES)

    def test_visual_tools_appear_only_when_vision_capable(self) -> None:
        registry = ToolRegistry(self.browser, vision_capable=True)
        names = {s["name"] for s in registry.schemas()}
        self.assertTrue(VISUAL_TOOL_NAMES.issubset(names))

    def test_structured_tools_are_always_offered_regardless_of_vision(self) -> None:
        """Structured tools are never conditional on vision - only the
        fallback is. This is the "prefer structured tools first" property
        made concrete: a text-only provider loses nothing but the
        fallback."""
        for vision in (False, True):
            registry = ToolRegistry(self.browser, vision_capable=vision)
            names = {s["name"] for s in registry.schemas()}
            self.assertIn("browser_click", names)
            self.assertIn("browser_get_page", names)

    def test_a_visual_tool_is_unknown_to_a_non_vision_registry(self) -> None:
        registry = ToolRegistry(self.browser, vision_capable=False)
        self.assertFalse(registry.knows("browser_visual_click"))

    def test_a_visual_tool_is_known_to_a_vision_capable_registry(self) -> None:
        registry = ToolRegistry(self.browser, vision_capable=True)
        self.assertTrue(registry.knows("browser_visual_observe"))

    def test_every_visual_tool_name_is_a_real_registered_tool(self) -> None:
        self.assertTrue(VISUAL_TOOL_NAMES.issubset(TOOL_NAMES))

    def test_run_refuses_a_visual_tool_without_vision_even_called_directly(self) -> None:
        """Defense in depth, same shape as the Skill allowlist check: even
        a caller that skips schemas()/knows() and calls run() straight is
        still refused - never silently executed just because the caller
        happened to bypass the model-facing gate."""
        registry = ToolRegistry(self.browser, vision_capable=False)
        with self.assertRaises(ToolError):
            registry.run("browser_visual_observe", {})


class VisualToolExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, _server.base)
        self.tabs.resize(1200, 900)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()
        result = self.browser.navigate(_server.url("/visual")).wait()
        self.assertTrue(result.ok, result.error)
        self.registry = ToolRegistry(self.browser, vision_capable=True)

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def test_observe_returns_an_image_when_vision_capable(self) -> None:
        outcome = self.registry.run("browser_visual_observe", {})
        self.assertIsNotNone(outcome.image)
        self.assertTrue(outcome.immediate["ok"])
        self.assertIn("viewport_width", outcome.immediate)

    def test_observe_is_refused_outright_without_vision_support(self) -> None:
        registry = ToolRegistry(self.browser, vision_capable=False)
        with self.assertRaises(ToolError):
            registry.run("browser_visual_observe", {})

    def test_a_harmless_visual_click_proceeds_without_confirmation(self) -> None:
        outcome = self.registry.run(
            "browser_visual_click", {"x": NORMAL_BUTTON[0], "y": NORMAL_BUTTON[1]})
        result = outcome.future.wait()
        self.assertTrue(result.ok, result.error)

    def test_a_sensitive_looking_visual_click_is_refused_without_confirmation(self) -> None:
        outcome = self.registry.run(
            "browser_visual_click", {"x": BUY_BUTTON[0], "y": BUY_BUTTON[1]})
        result = outcome.future.wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "CONFIRMATION_REQUIRED")
        self.assertTrue(result.sensitivity.get("requires_confirmation"))

    def test_the_same_sensitive_click_proceeds_once_confirmed(self) -> None:
        outcome = self.registry.run(
            "browser_visual_click",
            {"x": BUY_BUTTON[0], "y": BUY_BUTTON[1], "confirmed": True})
        result = outcome.future.wait()
        self.assertTrue(result.ok, result.error)

    def test_typing_into_a_password_field_is_refused_without_confirmation(self) -> None:
        focus = self.registry.run(
            "browser_visual_focus", {"x": PASSWORD_FIELD[0], "y": PASSWORD_FIELD[1]})
        focus.future.wait()
        outcome = self.registry.run("browser_visual_type", {"text": "hunter2"})
        result = outcome.future.wait()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "CONFIRMATION_REQUIRED")

    def test_typing_into_an_ordinary_field_needs_no_confirmation(self) -> None:
        focus = self.registry.run(
            "browser_visual_focus", {"x": TEXT_FIELD[0], "y": TEXT_FIELD[1]})
        focus.future.wait()
        outcome = self.registry.run("browser_visual_type", {"text": "hello"})
        result = outcome.future.wait()
        self.assertTrue(result.ok, result.error)

    def test_an_unrecognised_tool_name_fails_closed_via_run(self) -> None:
        with self.assertRaises(ToolError):
            self.registry.run("browser_visual_teleport", {"x": 1, "y": 1})

    def test_visual_scroll_behaves_like_the_structured_scroll_tool(self) -> None:
        outcome = self.registry.run("browser_visual_scroll", {"direction": "down"})
        result = outcome.future.wait()
        self.assertTrue(result.ok, result.error)

    def test_visual_action_budget_is_enforced(self) -> None:
        for _ in range(12):
            self.registry.visual_budget.record_action()
        outcome = self.registry.run(
            "browser_visual_click", {"x": NORMAL_BUTTON[0], "y": NORMAL_BUTTON[1]})
        self.assertIsNotNone(outcome.immediate)
        self.assertFalse(outcome.immediate["ok"])
        self.assertEqual(outcome.immediate["error"]["code"], "VISUAL_BUDGET_EXCEEDED")

    def test_visual_screenshot_budget_is_enforced(self) -> None:
        for _ in range(10):
            self.registry.visual_budget.record_screenshot()
        outcome = self.registry.run("browser_visual_observe", {})
        self.assertFalse(outcome.immediate["ok"])
        self.assertEqual(outcome.immediate["error"]["code"], "VISUAL_BUDGET_EXCEEDED")

    def test_clicking_nothing_at_all_is_a_clean_failure_not_a_blind_act(self) -> None:
        width, height = self.browser.viewport_size()
        outcome = self.registry.run(
            "browser_visual_click", {"x": width + 500, "y": height + 500})
        result = outcome.future.wait()
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
