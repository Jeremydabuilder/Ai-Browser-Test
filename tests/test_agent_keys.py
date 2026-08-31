"""Unit tests for API key handling. No Qt, no network."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import keys  # noqa: E402


class _Panic(BaseException):
    """Stands in for pyo3_runtime.PanicException, which is NOT an Exception."""


class KeySourceTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop(keys.ENV_VAR, None)

    def tearDown(self):
        os.environ.pop(keys.ENV_VAR, None)
        if self._env is not None:
            os.environ[keys.ENV_VAR] = self._env

    def test_environment_variable_is_used_when_there_is_no_keyring(self):
        os.environ[keys.ENV_VAR] = "  sk-ant-test-value  "
        with mock.patch.object(keys, "_keyring", side_effect=keys.KeyringUnavailable("none")):
            self.assertEqual(keys.ApiKeyStore().get_key(), "sk-ant-test-value")

    def test_keyring_wins_over_the_environment(self):
        os.environ[keys.ENV_VAR] = "from-env"
        fake = mock.Mock()
        fake.get_password.return_value = "from-keyring"
        with mock.patch.object(keys, "_keyring", return_value=fake):
            self.assertEqual(keys.ApiKeyStore().get_key(), "from-keyring")

    def test_no_key_anywhere(self):
        with mock.patch.object(keys, "_keyring", side_effect=keys.KeyringUnavailable("none")):
            store = keys.ApiKeyStore()
            self.assertIsNone(store.get_key())
            self.assertFalse(store.describe().available)

    def test_describe_never_reveals_any_part_of_the_key(self):
        os.environ[keys.ENV_VAR] = "sk-ant-super-secret-abcdef"
        with mock.patch.object(keys, "_keyring", side_effect=keys.KeyringUnavailable("none")):
            source = keys.ApiKeyStore().describe()
        self.assertTrue(source.available)
        rendered = f"{source.origin} {source.detail}"
        self.assertNotIn("secret", rendered)
        self.assertNotIn("abcdef", rendered)
        self.assertNotIn("sk-ant", rendered)

    def test_a_keyring_that_panics_does_not_crash_the_caller(self):
        """Regression: a broken `cryptography` raises a Rust panic.

        pyo3_runtime.PanicException derives from BaseException, so an
        `except Exception` guard misses it - which took the whole application
        down when the agent panel was opened.
        """
        os.environ[keys.ENV_VAR] = "fallback-key"
        with mock.patch.object(keys, "_keyring", side_effect=_Panic("Python API call failed")):
            store = keys.ApiKeyStore()
            self.assertEqual(store.get_key(), "fallback-key")   # must not raise
            self.assertTrue(store.describe().available)
            self.assertFalse(store.keyring_available())

    def test_a_panicking_keyring_still_reports_unavailable_for_writes(self):
        with mock.patch.object(keys, "_keyring", side_effect=_Panic("boom")):
            with self.assertRaises(keys.KeyringUnavailable):
                keys.ApiKeyStore().set_key("sk-ant-whatever")

    def test_keyboard_interrupt_is_never_swallowed(self):
        with mock.patch.object(keys, "_keyring", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                keys.ApiKeyStore().get_key()

    def test_empty_key_is_rejected(self):
        with self.assertRaises(ValueError):
            keys.ApiKeyStore().set_key("   ")

    def test_nothing_writes_the_key_to_the_project(self):
        """The store must not persist anything itself."""
        fake = mock.Mock()
        with mock.patch.object(keys, "_keyring", return_value=fake):
            keys.ApiKeyStore().set_key("sk-ant-value")
        fake.set_password.assert_called_once()
        # It went to the keyring and nowhere else.
        self.assertEqual(fake.set_password.call_args[0][0], keys.SERVICE_NAME)


if __name__ == "__main__":
    unittest.main()
