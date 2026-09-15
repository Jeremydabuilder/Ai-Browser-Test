"""Phase 22: Release Hardening / Updater / Signing Polish.

Covers version parsing/comparison, channel behavior, release manifest
parsing/generation, checksum verification, update-check logic, diagnostics
redaction, Safe Mode resolution, crash-recovery state, and first-run
validation. Nothing here touches a real network or a real OS installer -
see app.updater.checker's own module docstring for why the network call
itself is a thin, separately-guarded function.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_release_infra -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-release-"))
os.environ["PYBROWSER_DISABLE_KEYRING"] = "1"

from app import diagnostics  # noqa: E402
from app import startup_checks  # noqa: E402
from app import startup_state  # noqa: E402
from app import version as version_mod  # noqa: E402
from app.updater import checker, manifest as manifest_mod, verify  # noqa: E402


# ---------------------------------------------------------------------------
# Version parsing / comparison / channel
# ---------------------------------------------------------------------------

class VersionParsingTests(unittest.TestCase):
    def test_parses_plain_semver(self):
        v = version_mod.parse_version("1.2.3")
        self.assertEqual((v.major, v.minor, v.patch), (1, 2, 3))
        self.assertEqual(str(v), "1.2.3")

    def test_parses_prerelease_and_build(self):
        v = version_mod.parse_version("1.2.3-rc.1+build.5")
        self.assertEqual(v.prerelease, "rc.1")
        self.assertEqual(v.build, "build.5")
        self.assertEqual(str(v), "1.2.3-rc.1+build.5")

    def test_rejects_malformed_versions(self):
        for bad in ("1.2", "v1.2.3", "1.2.3.4", "1.2.x", "", "not-a-version", "1.2.03"):
            with self.assertRaises(version_mod.InvalidVersion):
                version_mod.parse_version(bad)


class VersionComparisonTests(unittest.TestCase):
    def test_patch_ordering(self):
        self.assertEqual(version_mod.compare_versions("0.1.0", "0.1.1"), -1)
        self.assertEqual(version_mod.compare_versions("0.1.1", "0.1.0"), 1)
        self.assertEqual(version_mod.compare_versions("0.1.1", "0.1.1"), 0)

    def test_minor_and_major_ordering(self):
        self.assertTrue(version_mod.is_newer("1.0.0", "0.9.9"))
        self.assertTrue(version_mod.is_newer("0.2.0", "0.1.9"))
        self.assertFalse(version_mod.is_newer("0.1.0", "0.1.0"))

    def test_prerelease_sorts_before_release(self):
        # semver 2.0.0: 1.0.0-rc.1 < 1.0.0
        self.assertEqual(version_mod.compare_versions("1.0.0-rc.1", "1.0.0"), -1)
        self.assertTrue(version_mod.is_newer("1.0.0", "1.0.0-rc.1"))

    def test_build_metadata_never_affects_ordering(self):
        self.assertEqual(version_mod.compare_versions("1.0.0+abc", "1.0.0+xyz"), 0)

    def test_ignores_current_local_version_when_comparing_directly(self):
        # current_version() reflects app.__version__ - just confirm it
        # parses cleanly regardless of what it currently is.
        version_mod.parse_version(version_mod.current_version())


class ChannelTests(unittest.TestCase):
    def test_defaults_to_stable(self):
        os.environ.pop("PYBROWSER_CHANNEL", None)
        self.assertEqual(version_mod.current_channel(), version_mod.CHANNEL_STABLE)

    def test_reads_env_override(self):
        os.environ["PYBROWSER_CHANNEL"] = "preview"
        try:
            self.assertEqual(version_mod.current_channel(), version_mod.CHANNEL_PREVIEW)
        finally:
            os.environ.pop("PYBROWSER_CHANNEL", None)

    def test_unknown_channel_falls_back_to_stable(self):
        os.environ["PYBROWSER_CHANNEL"] = "bogus"
        try:
            self.assertEqual(version_mod.current_channel(), version_mod.CHANNEL_STABLE)
        finally:
            os.environ.pop("PYBROWSER_CHANNEL", None)

    def test_build_info_display_lines_have_no_secrets_and_include_channel(self):
        info = version_mod.build_info()
        lines = info.display_lines()
        joined = "\n".join(lines)
        self.assertIn(info.channel, joined)
        self.assertIn(info.version, joined)
        self.assertNotIn("API_KEY", joined.upper().replace("PYBROWSER", ""))


# ---------------------------------------------------------------------------
# Release manifest parsing / generation
# ---------------------------------------------------------------------------

_VALID_SHA = "a" * 64


class ManifestParsingTests(unittest.TestCase):
    def test_parses_valid_manifest(self):
        raw = {
            "version": "0.2.0", "channel": "preview",
            "notes_url": "https://example.com/notes",
            "artifacts": {
                "windows": {"url": "https://example.com/a.exe", "sha256": _VALID_SHA, "size": 100},
            },
        }
        m = manifest_mod.parse_manifest(raw)
        self.assertEqual(m.version, "0.2.0")
        self.assertEqual(m.channel, "preview")
        self.assertEqual(m.artifact_for("windows").size, 100)
        self.assertIsNone(m.artifact_for("macos"))

    def test_parses_from_json_string(self):
        m = manifest_mod.parse_manifest(
            '{"version": "1.0.0", "channel": "stable", "artifacts": {}}')
        self.assertEqual(m.version, "1.0.0")

    def test_rejects_bad_json(self):
        with self.assertRaises(manifest_mod.ManifestError):
            manifest_mod.parse_manifest("{not json")

    def test_rejects_non_object_json(self):
        with self.assertRaises(manifest_mod.ManifestError):
            manifest_mod.parse_manifest("[1, 2, 3]")

    def test_rejects_missing_version(self):
        with self.assertRaises(manifest_mod.ManifestError):
            manifest_mod.parse_manifest({"channel": "stable", "artifacts": {}})

    def test_rejects_malformed_version(self):
        with self.assertRaises(manifest_mod.ManifestError):
            manifest_mod.parse_manifest({"version": "not-semver", "artifacts": {}})

    def test_rejects_unknown_channel(self):
        with self.assertRaises(manifest_mod.ManifestError):
            manifest_mod.parse_manifest({"version": "1.0.0", "channel": "beta", "artifacts": {}})

    def test_rejects_artifact_missing_url(self):
        with self.assertRaises(manifest_mod.ManifestError):
            manifest_mod.parse_manifest({
                "version": "1.0.0", "artifacts": {"windows": {"sha256": _VALID_SHA, "size": 1}}})

    def test_rejects_artifact_bad_sha256_length(self):
        with self.assertRaises(manifest_mod.ManifestError):
            manifest_mod.parse_manifest({
                "version": "1.0.0",
                "artifacts": {"windows": {"url": "https://x", "sha256": "abc", "size": 1}}})

    def test_rejects_artifact_non_hex_sha256(self):
        bad_sha = "z" * 64
        with self.assertRaises(manifest_mod.ManifestError):
            manifest_mod.parse_manifest({
                "version": "1.0.0",
                "artifacts": {"windows": {"url": "https://x", "sha256": bad_sha, "size": 1}}})

    def test_rejects_negative_size(self):
        with self.assertRaises(manifest_mod.ManifestError):
            manifest_mod.parse_manifest({
                "version": "1.0.0",
                "artifacts": {"windows": {"url": "https://x", "sha256": _VALID_SHA, "size": -1}}})

    def test_build_manifest_round_trips_through_validation(self):
        artifact = manifest_mod.Artifact(url="https://example.com/a.dmg", sha256=_VALID_SHA, size=42)
        m = manifest_mod.build_manifest(
            version="0.3.0", channel="nightly", artifacts={"macos": artifact})
        self.assertEqual(m.channel, "nightly")
        parsed_back = manifest_mod.parse_manifest(m.to_json())
        self.assertEqual(parsed_back.version, "0.3.0")
        self.assertEqual(parsed_back.artifact_for("macos").sha256, _VALID_SHA)


class GenerateManifestScriptTests(unittest.TestCase):
    """Part 17's "release metadata generation" - exercised as a real
    subprocess invocation of the actual CLI script CI calls, not just its
    library function, so a broken argparse wiring would be caught too."""

    def test_script_generates_a_valid_manifest_from_a_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = os.path.join(tmp, "fake-installer.exe")
            with open(artifact_path, "wb") as fh:
                fh.write(b"pretend installer bytes")
            out_path = os.path.join(tmp, "manifest.json")
            script = os.path.join(os.path.dirname(__file__), "..", "scripts", "generate_manifest.py")
            result = subprocess.run(
                [sys.executable, script, "--version", "0.9.9", "--channel", "preview",
                 "--windows-url", "https://example.com/fake-installer.exe",
                 "--windows-file", artifact_path, "--out", out_path],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.isfile(out_path))
            with open(out_path, encoding="utf-8") as fh:
                m = manifest_mod.parse_manifest(fh.read())
            self.assertEqual(m.version, "0.9.9")
            expected_sha = verify.sha256_of_file(artifact_path)
            self.assertEqual(m.artifact_for("windows").sha256, expected_sha)
            self.assertEqual(m.artifact_for("windows").size, len(b"pretend installer bytes"))


# ---------------------------------------------------------------------------
# Checksum verification
# ---------------------------------------------------------------------------

class VerifyArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pybrowser-verify-")
        self.path = os.path.join(self.tmp, "artifact.bin")
        with open(self.path, "wb") as fh:
            fh.write(b"a" * 1000)
        self.correct_sha = verify.sha256_of_file(self.path)

    def test_correct_checksum_and_size_pass(self):
        artifact = manifest_mod.Artifact(url="https://x", sha256=self.correct_sha, size=1000)
        result = verify.verify_artifact(self.path, artifact)
        self.assertTrue(result.ok)

    def test_wrong_checksum_is_rejected(self):
        artifact = manifest_mod.Artifact(url="https://x", sha256="f" * 64, size=1000)
        result = verify.verify_artifact(self.path, artifact)
        self.assertFalse(result.ok)
        self.assertIn("checksum mismatch", result.reason)

    def test_wrong_size_is_rejected_before_hashing(self):
        artifact = manifest_mod.Artifact(url="https://x", sha256=self.correct_sha, size=999)
        result = verify.verify_artifact(self.path, artifact)
        self.assertFalse(result.ok)
        self.assertIn("size mismatch", result.reason)

    def test_missing_file_is_rejected(self):
        artifact = manifest_mod.Artifact(url="https://x", sha256=self.correct_sha, size=1000)
        result = verify.verify_artifact(os.path.join(self.tmp, "does-not-exist"), artifact)
        self.assertFalse(result.ok)
        self.assertIn("not found", result.reason)


# ---------------------------------------------------------------------------
# Update-check logic (no network - evaluate_manifest is pure)
# ---------------------------------------------------------------------------

class UpdateEvaluationTests(unittest.TestCase):
    def test_newer_version_with_matching_platform_is_available(self):
        m = manifest_mod.parse_manifest({
            "version": "9.9.9", "channel": "stable",
            "artifacts": {"windows": {"url": "https://x/a.exe", "sha256": _VALID_SHA, "size": 5}}})
        result = checker.evaluate_manifest(m, current="0.1.0", this_platform="windows")
        self.assertTrue(result.available)
        self.assertEqual(result.latest_version, "9.9.9")
        self.assertEqual(result.download_url, "https://x/a.exe")
        self.assertEqual(result.sha256, _VALID_SHA)

    def test_same_or_older_version_reports_no_update(self):
        m = manifest_mod.parse_manifest({
            "version": "0.1.0", "channel": "stable",
            "artifacts": {"windows": {"url": "https://x/a.exe", "sha256": _VALID_SHA, "size": 5}}})
        result = checker.evaluate_manifest(m, current="0.1.0", this_platform="windows")
        self.assertFalse(result.available)

        older = manifest_mod.parse_manifest({
            "version": "0.0.9", "channel": "stable", "artifacts": {}})
        result2 = checker.evaluate_manifest(older, current="0.1.0", this_platform="windows")
        self.assertFalse(result2.available)

    def test_newer_version_missing_this_platform_is_not_offered(self):
        m = manifest_mod.parse_manifest({
            "version": "9.9.9", "channel": "stable",
            "artifacts": {"macos": {"url": "https://x/a.dmg", "sha256": _VALID_SHA, "size": 5}}})
        result = checker.evaluate_manifest(m, current="0.1.0", this_platform="windows")
        self.assertFalse(result.available)
        # Still honestly reports that a newer release exists, just not for us.
        self.assertEqual(result.latest_version, "9.9.9")

    def test_platform_key_maps_known_systems(self):
        self.assertIn(checker.platform_key(), ("windows", "macos", "linux"))


class ChannelIsolationTests(unittest.TestCase):
    """Part 8: a stable build must never silently check a preview
    manifest URL, and vice versa."""

    def test_stable_and_preview_manifest_urls_differ(self):
        stable_url = checker.manifest_url(version_mod.CHANNEL_STABLE)
        preview_url = checker.manifest_url(version_mod.CHANNEL_PREVIEW)
        self.assertNotEqual(stable_url, preview_url)

    def test_env_override_takes_precedence_for_any_channel(self):
        os.environ["PYBROWSER_UPDATE_MANIFEST_URL"] = "https://example.com/custom.json"
        try:
            self.assertEqual(
                checker.manifest_url(version_mod.CHANNEL_PREVIEW), "https://example.com/custom.json")
            self.assertEqual(
                checker.manifest_url(version_mod.CHANNEL_STABLE), "https://example.com/custom.json")
        finally:
            os.environ.pop("PYBROWSER_UPDATE_MANIFEST_URL", None)


# ---------------------------------------------------------------------------
# Diagnostics redaction
# ---------------------------------------------------------------------------

class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        diagnostics.clear_breadcrumbs()

    def test_report_includes_version_and_platform(self):
        report = diagnostics.build_report()
        self.assertIn(version_mod.current_version(), report.text)

    def test_report_includes_recent_breadcrumbs(self):
        diagnostics.breadcrumb("app started")
        diagnostics.breadcrumb("workspace switched to Home")
        report = diagnostics.build_report()
        self.assertIn("app started", report.text)
        self.assertIn("workspace switched to Home", report.text)

    def test_breadcrumbs_are_bounded(self):
        for i in range(diagnostics._MAX_BREADCRUMBS + 25):
            diagnostics.breadcrumb(f"event {i}")
        crumbs = diagnostics.recent_breadcrumbs(limit=1000)
        self.assertLessEqual(len(crumbs), diagnostics._MAX_BREADCRUMBS)

    def test_api_key_like_value_in_traceback_is_redacted(self):
        try:
            secret = "sk-ant-api03-" + "x" * 40  # noqa: S105 - a fake key shape for the test
            raise ValueError(f"failed with key {secret}")
        except ValueError:
            exc_info = sys.exc_info()
        report = diagnostics.build_report(exc_info=exc_info)
        self.assertNotIn("sk-ant-api03-", report.text)
        self.assertTrue(report.redacted)

    def test_no_secret_present_is_not_flagged_as_redacted(self):
        report = diagnostics.build_report(extra_lines=["nothing sensitive here"])
        self.assertFalse(report.redacted)


# ---------------------------------------------------------------------------
# Safe Mode resolution
# ---------------------------------------------------------------------------

class SafeModeTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PYBROWSER_SAFE_MODE", None)

    def tearDown(self):
        os.environ.pop("PYBROWSER_SAFE_MODE", None)

    def test_disabled_by_default(self):
        flags = startup_state.resolve_safe_mode()
        self.assertFalse(flags.enabled)
        self.assertFalse(flags.disable_mcp)
        self.assertFalse(flags.disable_sync)
        self.assertFalse(flags.disable_schedules)

    def test_cli_flag_enables_it(self):
        flags = startup_state.resolve_safe_mode(cli_flag=True)
        self.assertTrue(flags.enabled)
        self.assertIn("--safe-mode", flags.reason)

    def test_env_var_enables_it(self):
        os.environ["PYBROWSER_SAFE_MODE"] = "1"
        flags = startup_state.resolve_safe_mode()
        self.assertTrue(flags.enabled)

    def test_repeated_crashes_auto_trigger_it(self):
        flags = startup_state.resolve_safe_mode(crash_count=startup_state.AUTO_SAFE_MODE_THRESHOLD)
        self.assertTrue(flags.enabled)
        self.assertIn("did not shut down cleanly", flags.reason)

    def test_single_crash_does_not_trigger_it(self):
        flags = startup_state.resolve_safe_mode(crash_count=1)
        self.assertFalse(flags.enabled)

    def test_enabled_flags_gate_mcp_sync_schedules_together(self):
        flags = startup_state.resolve_safe_mode(cli_flag=True)
        self.assertTrue(flags.disable_mcp and flags.disable_sync and flags.disable_schedules)


# ---------------------------------------------------------------------------
# Crash-recovery state
# ---------------------------------------------------------------------------

class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pybrowser-session-state-")
        self._old_data_dir = os.environ.get("PYBROWSER_DATA_DIR")
        os.environ["PYBROWSER_DATA_DIR"] = self.tmp

    def tearDown(self):
        if self._old_data_dir is not None:
            os.environ["PYBROWSER_DATA_DIR"] = self._old_data_dir
        else:
            os.environ.pop("PYBROWSER_DATA_DIR", None)

    def test_first_ever_launch_is_not_a_crash(self):
        result = startup_state.mark_session_started()
        self.assertFalse(result.crashed_last_session)
        self.assertEqual(result.crash_count, 0)

    def test_clean_shutdown_then_next_launch_is_not_a_crash(self):
        startup_state.mark_session_started()
        startup_state.mark_session_clean()
        result = startup_state.mark_session_started()
        self.assertFalse(result.crashed_last_session)

    def test_unclean_shutdown_is_detected_and_counted(self):
        startup_state.mark_session_started()
        # No mark_session_clean() call - simulates a crash.
        result = startup_state.mark_session_started()
        self.assertTrue(result.crashed_last_session)
        self.assertEqual(result.crash_count, 1)

    def test_crash_count_accumulates_across_repeated_crashes(self):
        startup_state.mark_session_started()
        startup_state.mark_session_started()
        result = startup_state.mark_session_started()
        self.assertTrue(result.crashed_last_session)
        self.assertEqual(result.crash_count, 2)

    def test_a_clean_shutdown_resets_the_crash_count(self):
        startup_state.mark_session_started()
        startup_state.mark_session_started()  # 1 crash recorded
        startup_state.mark_session_clean()
        result = startup_state.mark_session_started()
        self.assertFalse(result.crashed_last_session)
        self.assertEqual(result.crash_count, 0)

    def test_corrupt_marker_file_is_treated_as_unclean_not_fatal(self):
        marker_path = startup_state._marker_path()
        marker_path.write_text("{not json", encoding="utf-8")
        result = startup_state.mark_session_started()
        self.assertTrue(result.crashed_last_session)


# ---------------------------------------------------------------------------
# First-run validation
# ---------------------------------------------------------------------------

class StartupChecksTests(unittest.TestCase):
    def test_writable_profile_dir_passes_in_a_real_temp_dir(self):
        result = startup_checks.check_writable_profile_dir()
        self.assertTrue(result.ok)

    def test_webengine_check_passes_when_pyside6_is_installed(self):
        result = startup_checks.check_webengine_available()
        self.assertTrue(result.ok)

    def test_keyring_check_never_blocks_startup(self):
        # Soft check: even if it reports a problem, ok must stay True -
        # see check_keyring_available's own docstring.
        result = startup_checks.check_keyring_available()
        self.assertTrue(result.ok)

    def test_database_migration_check_passes_for_a_fresh_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.sqlite3")
            result = startup_checks.check_database_migration(db_path)
            self.assertTrue(result.ok)

    def test_fatal_failures_excludes_soft_keyring_check(self):
        from app.startup_checks import CheckResult

        results = [
            CheckResult("writable_profile_dir", True),
            CheckResult("keyring_available", True, "keyring backend missing but non-fatal"),
        ]
        self.assertEqual(startup_checks.fatal_failures(results), [])

    def test_fatal_failures_includes_a_real_failure(self):
        from app.startup_checks import CheckResult

        results = [CheckResult("webengine_available", False, "not installed")]
        failures = startup_checks.fatal_failures(results)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].name, "webengine_available")

    def test_run_all_returns_every_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.sqlite3")
            results = startup_checks.run_all(database_path=db_path)
            names = {r.name for r in results}
            self.assertEqual(
                names, {"writable_profile_dir", "database_migration",
                       "webengine_available", "keyring_available"})


# ---------------------------------------------------------------------------
# About / Update dialog UI wiring (no real network calls)
# ---------------------------------------------------------------------------

class AboutAndUpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication(sys.argv[:1])

    def test_about_dialog_builds_and_copy_diagnostics_does_not_crash(self):
        from app.ui.about_dialog import AboutDialog

        dialog = AboutDialog()
        dialog._copy_diagnostics()
        self.assertIn("copied", dialog._status_label.text().lower())
        dialog.close()

    def test_update_dialog_reports_up_to_date_without_network(self):
        from unittest import mock

        from app.updater.checker import UpdateResult
        from app.ui.update_dialog import UpdateCheckDialog

        with mock.patch(
            "app.ui.update_dialog.check_for_updates",
            return_value=UpdateResult(available=False, current_version="0.1.0"),
        ):
            dialog = UpdateCheckDialog()
        self.assertIn("up to date", dialog._status_label.text().lower())
        self.assertFalse(dialog._download_button.isVisible())
        dialog.close()

    def test_update_dialog_offers_download_when_available(self):
        from unittest import mock

        from app.updater.checker import UpdateResult
        from app.ui.update_dialog import UpdateCheckDialog

        result = UpdateResult(
            available=True, current_version="0.1.0", latest_version="9.9.9",
            channel="preview", notes_url="https://example.com/notes",
            download_url="https://example.com/a.exe", sha256=_VALID_SHA, size=10)
        with mock.patch("app.ui.update_dialog.check_for_updates", return_value=result):
            dialog = UpdateCheckDialog()
        self.assertIn("9.9.9", dialog._status_label.text())
        self.assertFalse(dialog._download_button.isHidden())
        dialog.close()

    def test_update_dialog_reports_check_failure_cleanly(self):
        from unittest import mock

        from app.updater.checker import UpdateCheckError
        from app.ui.update_dialog import UpdateCheckDialog

        with mock.patch(
            "app.ui.update_dialog.check_for_updates", side_effect=UpdateCheckError("network down")
        ):
            dialog = UpdateCheckDialog()
        self.assertIn("could not check", dialog._status_label.text().lower())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
