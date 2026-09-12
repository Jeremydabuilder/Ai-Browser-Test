"""AgentSession.send(image=...): a screenshot or local image attached to a
user turn, shaped as an Anthropic-style content block - and the defensive
fallback in the OpenAI-compatible translator for a provider path that has
no image support wired up (see app/agent/config.py's
provider_supports_images).

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_image_session -v
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-image-session-tests-"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import (  # noqa: E402
    PROVIDER_ANTHROPIC, PROVIDER_GROQ, PROVIDER_OPENAI, AgentConfig, ContextLimits,
    provider_supports_images,
)
from app.agent.openai_compatible import messages_param  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fake_claude import ScriptedClaude, says  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def pump(predicate, timeout_ms: int = 15000) -> bool:
    from PySide6.QtCore import QTimer

    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class ProviderCapabilityTests(unittest.TestCase):
    def test_anthropic_supports_images(self) -> None:
        self.assertTrue(provider_supports_images(PROVIDER_ANTHROPIC))

    def test_openai_supports_images(self) -> None:
        self.assertTrue(provider_supports_images(PROVIDER_OPENAI))

    def test_groq_does_not_yet(self) -> None:
        self.assertFalse(provider_supports_images(PROVIDER_GROQ))

    def test_an_unknown_provider_id_falls_back_to_the_default_providers_answer(self) -> None:
        """describe_provider() itself falls back to PROVIDERS[0] (Anthropic)
        for an id it does not recognise - this just inherits that."""
        from app.agent.config import PROVIDERS

        self.assertEqual(provider_supports_images("something-made-up"),
                         PROVIDERS[0].supports_images)


class SendWithImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(800, 600)
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()
        self.session: AgentSession | None = None

    def tearDown(self) -> None:
        if self.session is not None:
            self.session.shutdown()
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def _start(self, script: list) -> ScriptedClaude:
        fake = ScriptedClaude(script)
        self.session = AgentSession(self.browser, fake, AgentConfig(limits=ContextLimits()))
        self.fake = fake
        return fake

    def test_an_image_turn_carries_text_and_image_blocks(self) -> None:
        fake = self._start([says("I see a red square.")])
        data = base64.b64encode(b"fake-png-bytes").decode("ascii")
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.assertTrue(self.session.send(
            "What is in this image?", image={"mime_type": "image/png", "data": data}))
        self.assertTrue(pump(lambda: bool(done)))
        last = fake.requests[-1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertIsInstance(last["content"], list)
        kinds = [block["type"] for block in last["content"]]
        self.assertEqual(kinds, ["text", "image"])
        self.assertEqual(last["content"][0]["text"], "What is in this image?")
        image_block = last["content"][1]
        self.assertEqual(image_block["source"]["media_type"], "image/png")
        self.assertEqual(image_block["source"]["data"], data)

    def test_a_plain_send_is_unaffected(self) -> None:
        """No image kwarg: content is still a plain string, exactly as before."""
        fake = self._start([says("ok")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.assertTrue(self.session.send("Just text."))
        self.assertTrue(pump(lambda: bool(done)))
        self.assertEqual(fake.requests[-1]["messages"][-1]["content"], "Just text.")


class OpenAICompatibleImageFallbackTests(unittest.TestCase):
    """A provider path with no image support must never silently drop the
    turn - see _user_content_turn in app/agent/openai_compatible.py."""

    def test_an_image_block_becomes_a_visible_placeholder_not_silence(self) -> None:
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Look at this."},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "Zm9v"}},
        ]}]
        out = messages_param("system prompt", messages)
        user_turn = out[-1]
        self.assertEqual(user_turn["role"], "user")
        self.assertIn("Look at this.", user_turn["content"])
        self.assertIn("not configured to receive images", user_turn["content"])

    def test_a_text_only_turn_is_unaffected(self) -> None:
        messages = [{"role": "user", "content": "Hello"}]
        out = messages_param("system", messages)
        self.assertEqual(out[-1], {"role": "user", "content": "Hello"})

    def test_supports_images_true_builds_an_image_url_data_uri(self) -> None:
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Look at this."},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "Zm9v"}},
        ]}]
        out = messages_param("system", messages, supports_images=True)
        user_turn = out[-1]
        self.assertEqual(user_turn["role"], "user")
        self.assertIsInstance(user_turn["content"], list)
        kinds = [part["type"] for part in user_turn["content"]]
        self.assertEqual(kinds, ["text", "image_url"])
        self.assertEqual(user_turn["content"][0]["text"], "Look at this.")
        self.assertEqual(user_turn["content"][1]["image_url"]["url"],
                         "data:image/png;base64,Zm9v")

    def test_openai_client_declares_image_support(self) -> None:
        from app.agent.openai_compatible import OpenAIClient

        self.assertTrue(OpenAIClient.SUPPORTS_IMAGES)

    def test_groq_and_gemini_and_openrouter_do_not(self) -> None:
        from app.agent.openai_compatible import GeminiClient, GroqClient, OpenRouterClient

        self.assertFalse(GroqClient.SUPPORTS_IMAGES)
        self.assertFalse(GeminiClient.SUPPORTS_IMAGES)
        self.assertFalse(OpenRouterClient.SUPPORTS_IMAGES)


if __name__ == "__main__":
    unittest.main()
