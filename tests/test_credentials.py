"""Unit tests for credential resolution. No Qt, no network, no real secrets."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import credentials as creds  # noqa: E402
from app.agent.claude_client import ClaudeClient, ClaudeError  # noqa: E402
from app.agent.config import AgentConfig  # noqa: E402

_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PYBROWSER_AGENT_BACKEND",
         "AWS_REGION", "AWS_DEFAULT_REGION", "PYBROWSER_AWS_REGION",
         "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_REGION", "PYBROWSER_GCP_PROJECT",
         "PYBROWSER_GCP_REGION", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR")


class _Store:
    """An ApiKeyStore stand-in, so tests never touch a real keyring."""

    def __init__(self, key=None, broken=False):
        self._key, self._broken = key, broken

    def get_keyring_key(self):
        if self._broken:
            raise RuntimeError("keyring backend exploded")
        return self._key


class CredentialResolutionTests(unittest.TestCase):
    def setUp(self):
        self._saved = {v: os.environ.pop(v, None) for v in _VARS}
        # Assume no OAuth profile unless a test says otherwise.
        self._patch = mock.patch.object(creds, "_has_oauth_profile", return_value=False)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        for name, value in self._saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    # -- nothing configured ----------------------------------------------
    def test_nothing_configured(self):
        credential = creds.resolve(_Store())
        self.assertEqual(credential.mode, creds.Mode.NONE)
        self.assertFalse(credential.available)

    # -- an API key is NOT required --------------------------------------
    def test_an_oauth_profile_alone_is_enough(self):
        """The point of the exercise: no key anywhere, and the agent still runs."""
        with mock.patch.object(creds, "_has_oauth_profile", return_value=True):
            credential = creds.resolve(_Store())
        self.assertEqual(credential.mode, creds.Mode.OAUTH_PROFILE)
        self.assertTrue(credential.available)
        self.assertTrue(credential.needs_no_secret)
        self.assertIsNone(credential.secret)

    def test_a_bearer_token_alone_is_enough(self):
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "bearer-value"
        credential = creds.resolve(_Store())
        self.assertEqual(credential.mode, creds.Mode.AUTH_TOKEN)
        self.assertEqual(credential.secret, "bearer-value")

    def test_bedrock_needs_no_anthropic_secret(self):
        os.environ.update(PYBROWSER_AGENT_BACKEND="bedrock", AWS_REGION="eu-west-1")
        credential = creds.resolve(_Store())
        self.assertEqual(credential.mode, creds.Mode.BEDROCK)
        self.assertTrue(credential.needs_no_secret)
        self.assertIsNone(credential.secret)
        self.assertEqual(credential.region, "eu-west-1")

    def test_vertex_needs_no_anthropic_secret(self):
        os.environ.update(PYBROWSER_AGENT_BACKEND="vertex",
                          GOOGLE_CLOUD_PROJECT="proj", GOOGLE_CLOUD_REGION="us-east5")
        credential = creds.resolve(_Store())
        self.assertEqual(credential.mode, creds.Mode.VERTEX)
        self.assertTrue(credential.needs_no_secret)
        self.assertEqual(credential.project, "proj")

    def test_an_incomplete_cloud_backend_is_not_used(self):
        """Half-configured must fall through, not fail at request time."""
        os.environ["PYBROWSER_AGENT_BACKEND"] = "bedrock"     # no region
        self.assertEqual(creds.resolve(_Store()).mode, creds.Mode.NONE)
        os.environ["PYBROWSER_AGENT_BACKEND"] = "vertex"      # no project
        self.assertEqual(creds.resolve(_Store()).mode, creds.Mode.NONE)

    # -- precedence -------------------------------------------------------
    def test_an_explicit_cloud_backend_wins(self):
        os.environ.update(PYBROWSER_AGENT_BACKEND="bedrock", AWS_REGION="us-east-1",
                          ANTHROPIC_API_KEY="env-key")
        self.assertEqual(creds.resolve(_Store("keyring-key")).mode, creds.Mode.BEDROCK)

    def test_the_keyring_beats_the_environment(self):
        os.environ["ANTHROPIC_API_KEY"] = "env-key"
        credential = creds.resolve(_Store("keyring-key"))
        self.assertEqual(credential.mode, creds.Mode.KEYRING)
        self.assertEqual(credential.secret, "keyring-key")

    def test_an_env_key_beats_a_bearer_token(self):
        os.environ.update(ANTHROPIC_API_KEY="env-key", ANTHROPIC_AUTH_TOKEN="tok")
        self.assertEqual(creds.resolve(_Store()).mode, creds.Mode.ENV_KEY)

    def test_a_stored_key_beats_an_oauth_profile(self):
        """An explicit choice in this app outranks a discovered one."""
        with mock.patch.object(creds, "_has_oauth_profile", return_value=True):
            self.assertEqual(creds.resolve(_Store("keyring-key")).mode, creds.Mode.KEYRING)

    # -- robustness --------------------------------------------------------
    def test_a_broken_keyring_falls_through_rather_than_raising(self):
        os.environ["ANTHROPIC_API_KEY"] = "env-key"
        self.assertEqual(creds.resolve(_Store(broken=True)).mode, creds.Mode.ENV_KEY)

    def test_resolve_never_raises(self):
        with mock.patch.object(creds, "_has_oauth_profile", side_effect=RuntimeError("boom")):
            try:
                creds.resolve(_Store())
            except RuntimeError:
                self.fail("resolve() must never raise")
            except Exception:
                pass

    def test_no_credential_description_contains_a_secret(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-secret-value-here"
        credential = creds.resolve(_Store())
        self.assertNotIn("secret-value", credential.describe())
        self.assertNotIn("sk-ant", credential.describe())

    def test_options_summary_lists_every_way_in(self):
        modes = {mode for mode, _available, _help in creds.options_summary()}
        self.assertEqual(modes, {creds.Mode.OAUTH_PROFILE, creds.Mode.KEYRING,
                                 creds.Mode.ENV_KEY, creds.Mode.AUTH_TOKEN,
                                 creds.Mode.BEDROCK, creds.Mode.VERTEX})


class ClientConstructionTests(unittest.TestCase):
    """The client must build the right SDK object for each credential."""

    def test_a_plain_string_is_still_accepted(self):
        client = ClaudeClient("sk-ant-whatever", AgentConfig())
        self.assertEqual(client.credential.mode, creds.Mode.ENV_KEY)

    def test_an_unavailable_credential_is_refused_clearly(self):
        with self.assertRaises(ClaudeError):
            ClaudeClient(creds.Credential(creds.Mode.NONE, "nothing"), AgentConfig())

    def test_a_bearer_token_builds_a_client(self):
        client = ClaudeClient(
            creds.Credential(creds.Mode.AUTH_TOKEN, "token", secret="abc"), AgentConfig())
        self.assertIsNotNone(client._client)

    def test_bedrock_namespaces_the_model_id(self):
        credential = creds.Credential(creds.Mode.BEDROCK, "bedrock", region="us-east-1")
        self.assertEqual(ClaudeClient._model_id(credential, "claude-opus-5"),
                         "anthropic.claude-opus-5")

    def test_other_backends_keep_the_plain_model_id(self):
        for mode in (creds.Mode.KEYRING, creds.Mode.OAUTH_PROFILE, creds.Mode.VERTEX):
            credential = creds.Credential(mode, mode)
            self.assertEqual(ClaudeClient._model_id(credential, "claude-opus-5"),
                             "claude-opus-5")


if __name__ == "__main__":
    unittest.main()
