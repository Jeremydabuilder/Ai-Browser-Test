"""Py: the character, the states, the motion, and the promises made about them.

Py is a status indicator with a face, so the things worth testing are that the
face never says something the agent did not do, that the artwork can be
replaced without touching code, that motion stops when asked, and that nothing
from a web page can ever end up in what Py says.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mascot -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-mascot-"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.session import AgentState  # noqa: E402
from app.ui import mascot as mascot_module  # noqa: E402
from app.ui.mascot import (  # noqa: E402
    ALL_STATES,
    COMPANION_TEXT,
    TOOLTIPS,
    VARIANTS,
    Mascot,
    MascotState,
    Variant,
    asset_for,
    has_final_artwork,
    is_animated,
    reduced_motion,
    state_for_agent,
)

_app: QApplication | None = None

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
       '<rect width="10" height="10" fill="#6C5CE7"/></svg>')


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class _Assets:
    """Swap both asset folders for temporary ones, and put them back."""

    def __enter__(self):
        self._real = (mascot_module.ASSET_DIR, mascot_module.PLACEHOLDER_DIR)
        self.final = tempfile.mkdtemp(prefix="py-final-")
        self.placeholder = tempfile.mkdtemp(prefix="py-placeholder-")
        mascot_module.ASSET_DIR = self.final
        mascot_module.PLACEHOLDER_DIR = self.placeholder
        return self

    def __exit__(self, *_exc):
        mascot_module.ASSET_DIR, mascot_module.PLACEHOLDER_DIR = self._real
        shutil.rmtree(self.final, ignore_errors=True)
        shutil.rmtree(self.placeholder, ignore_errors=True)

    def write(self, directory: str, name: str, body: str = SVG) -> None:
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(body)


class ShippedArtworkTests(unittest.TestCase):
    """What the repository actually carries today."""

    def test_a_placeholder_exists_for_every_state_and_crop(self) -> None:
        for state in ALL_STATES:
            for variant in VARIANTS:
                path = asset_for(state, variant)
                self.assertIsNotNone(path, f"nothing to draw for {state}/{variant}")
                self.assertEqual(os.path.basename(path), f"{state}-{variant}.svg",
                                 f"{state}/{variant} fell back unexpectedly")

    def test_the_two_crops_are_different_drawings(self) -> None:
        # A bust is not a full body scaled down - cramming the whole figure
        # into a 40px slot is what this variant system exists to avoid.
        for state in ALL_STATES:
            full = asset_for(state, Variant.FULL)
            panel = asset_for(state, Variant.PANEL)
            self.assertNotEqual(full, panel, state)

    def test_the_placeholders_are_kept_apart_from_the_final_artwork(self) -> None:
        # The final Py drops into assets/mascot/; the stand-in lives one level
        # down so the two can never be confused for each other.
        self.assertFalse(has_final_artwork(),
                         "placeholders are being reported as the real artwork")
        for state in ALL_STATES:
            self.assertIn("placeholder", asset_for(state))

    def test_every_state_draws_something(self) -> None:
        mascot = Mascot(40)
        for state in ALL_STATES:
            mascot.set_state(state)
            self.assertFalse(mascot.pixmap().isNull(), f"{state} drew nothing")


class ArtworkResolutionTests(unittest.TestCase):
    """Dropping the real Py in must need no code change."""

    def test_final_artwork_beats_a_placeholder(self) -> None:
        with _Assets() as assets:
            assets.write(assets.placeholder, "idle.svg")
            assets.write(assets.final, "idle.svg")
            self.assertEqual(os.path.dirname(asset_for(MascotState.IDLE)), assets.final)
            self.assertTrue(has_final_artwork())

    def test_one_final_idle_outranks_a_full_set_of_placeholders(self) -> None:
        # This is what makes dropping in the real character feel immediate:
        # one file and Py is the new character everywhere.
        with _Assets() as assets:
            for state in ALL_STATES:
                assets.write(assets.placeholder, f"{state}.svg")
            assets.write(assets.final, "idle.svg")
            self.assertEqual(os.path.dirname(asset_for(MascotState.WORKING)),
                             assets.final)

    def test_a_state_prefers_its_own_file(self) -> None:
        with _Assets() as assets:
            assets.write(assets.final, "idle.svg")
            assets.write(assets.final, "thinking.svg")
            self.assertTrue(asset_for(MascotState.THINKING).endswith("thinking.svg"))

    def test_a_missing_state_falls_back_to_idle(self) -> None:
        with _Assets() as assets:
            assets.write(assets.final, "idle.svg")
            self.assertTrue(asset_for(MascotState.APPROVAL).endswith("idle.svg"))

    def test_not_every_state_has_to_exist(self) -> None:
        with _Assets() as assets:
            assets.write(assets.final, "idle.svg")
            assets.write(assets.final, "complete.svg")
            mascot = Mascot(40)
            for state in ALL_STATES:
                mascot.set_state(state)
                self.assertFalse(mascot.pixmap().isNull(), state)

    def test_with_no_artwork_at_all_something_is_still_drawn(self) -> None:
        with _Assets():
            self.assertIsNone(asset_for(MascotState.IDLE))
            mascot = Mascot(40)
            for state in ALL_STATES:
                mascot.set_state(state)
                self.assertFalse(mascot.pixmap().isNull(), state)

    def test_animated_formats_are_recognised(self) -> None:
        self.assertTrue(is_animated("/x/idle.gif"))
        self.assertTrue(is_animated("/x/IDLE.WEBP"))
        self.assertFalse(is_animated("/x/idle.svg"))
        self.assertFalse(is_animated("/x/idle.png"))

    def test_an_animated_file_wins_over_a_still_one(self) -> None:
        # So handing over an animated Py later replaces the built-in motion
        # without any code changing.
        with _Assets() as assets:
            assets.write(assets.final, "idle.png", "not really a png")
            assets.write(assets.final, "idle.gif", "not really a gif")
            self.assertTrue(asset_for(MascotState.IDLE).endswith(".gif"))

    def test_the_new_tab_page_uses_whatever_is_there(self) -> None:
        from app.browser.newtab import NewTabData, render

        # The shipped placeholder is already inlined.
        self.assertIn("data:image", render(NewTabData()))
        with _Assets():
            self.assertNotIn("data:image", render(NewTabData()))


class HonestyTests(unittest.TestCase):
    """Py must not claim something the agent did not do."""

    def test_thinking_reading_working_and_approval_map_straight_through(self) -> None:
        self.assertEqual(state_for_agent(AgentState.THINKING), MascotState.THINKING)
        self.assertEqual(state_for_agent(AgentState.ACTING), MascotState.WORKING)
        self.assertEqual(state_for_agent(AgentState.AWAITING_CONFIRMATION),
                         MascotState.APPROVAL)

    def test_complete_needs_an_actual_answer(self) -> None:
        self.assertEqual(state_for_agent(AgentState.IDLE, answered=True),
                         MascotState.COMPLETE)

    def test_a_task_that_answered_nothing_is_not_complete(self) -> None:
        self.assertEqual(state_for_agent(AgentState.IDLE, answered=False),
                         MascotState.IDLE)

    def test_a_failed_task_is_stuck_not_complete(self) -> None:
        self.assertEqual(
            state_for_agent(AgentState.IDLE, answered=True, failed=True),
            MascotState.STUCK,
            "Py celebrated a failure")

    def test_a_stopped_task_is_never_a_celebration(self) -> None:
        self.assertEqual(state_for_agent(AgentState.CANCELLING), MascotState.IDLE)
        self.assertEqual(state_for_agent(AgentState.IDLE, answered=True, failed=True),
                         MascotState.STUCK)

    def test_stuck_says_so_plainly(self) -> None:
        self.assertIn("stuck", COMPANION_TEXT[MascotState.STUCK].lower())
        self.assertNotIn("done", COMPANION_TEXT[MascotState.STUCK].lower())


class CompanionTextTests(unittest.TestCase):
    def test_every_state_has_something_to_say(self) -> None:
        for state in ALL_STATES:
            self.assertTrue(COMPANION_TEXT.get(state, "").strip(), state)
            self.assertTrue(TOOLTIPS.get(state, "").strip(), state)

    def test_the_lines_are_short_enough_to_read_at_a_glance(self) -> None:
        for state, line in COMPANION_TEXT.items():
            self.assertLessEqual(len(line), 40, f"{state}: {line!r}")

    def test_they_are_fixed_strings_and_cannot_carry_page_content(self) -> None:
        """The one security property this file is responsible for.

        Py's line is chosen by state alone. Nothing from a page, a tool result
        or the model can reach it, so no amount of hostile page content can put
        words in Py's mouth.
        """
        mascot = Mascot(40)
        for state in ALL_STATES:
            mascot.set_state(state)
            self.assertEqual(mascot.companion_text(), COMPANION_TEXT[state])

    def test_the_state_is_announced_for_a_screen_reader(self) -> None:
        mascot = Mascot(40)
        mascot.set_state(MascotState.APPROVAL)
        self.assertEqual(mascot.accessibleName(), "Py")
        self.assertIn("okay", mascot.accessibleDescription())
        self.assertIn("approval", mascot.toolTip())


class MotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("PYBROWSER_REDUCED_MOTION")
        os.environ.pop("PYBROWSER_REDUCED_MOTION", None)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("PYBROWSER_REDUCED_MOTION", None)
        else:
            os.environ["PYBROWSER_REDUCED_MOTION"] = self._saved

    def test_py_breathes_and_blinks_when_idle(self) -> None:
        mascot = Mascot(40)
        self.assertTrue(mascot._frames.isActive(),
                        "an idle Py should still be alive")

    def test_reduced_motion_stops_everything(self) -> None:
        os.environ["PYBROWSER_REDUCED_MOTION"] = "1"
        mascot = Mascot(40)
        for state in ALL_STATES:
            mascot.set_state(state)
            self.assertFalse(mascot._frames.isActive(),
                             f"{state} kept animating with reduced motion set")

    def test_reduced_motion_still_draws_every_state(self) -> None:
        os.environ["PYBROWSER_REDUCED_MOTION"] = "1"
        mascot = Mascot(40)
        for state in ALL_STATES:
            mascot.set_state(state)
            self.assertFalse(mascot.pixmap().isNull(), state)

    def test_a_reaction_settles_back_to_idle_by_itself(self) -> None:
        mascot = Mascot(40)
        for state in (MascotState.COMPLETE, MascotState.STUCK):
            mascot.set_state(MascotState.WORKING)
            mascot.set_state(state)
            self.assertTrue(mascot._settle.isActive(),
                            f"{state} would have stayed up forever")

    def test_a_working_state_does_not_settle_by_itself(self) -> None:
        mascot = Mascot(40)
        mascot.set_state(MascotState.WORKING)
        self.assertFalse(mascot._settle.isActive(),
                         "Py stopped working while the task was still running")

    def test_frames_advance_without_error(self) -> None:
        mascot = Mascot(40)
        for state in ALL_STATES:
            mascot.set_state(state)
            for _ in range(30):
                mascot._advance()
            self.assertFalse(mascot.pixmap().isNull(), state)


class SizingTests(unittest.TestCase):
    def test_it_can_be_resized_without_losing_its_state(self) -> None:
        mascot = Mascot(40)
        mascot.set_state(MascotState.READING)
        mascot.set_size(56)
        self.assertEqual(mascot.state(), MascotState.READING)
        self.assertEqual(mascot.width(), 56)
        self.assertFalse(mascot.pixmap().isNull())

    def test_a_nonsense_size_is_ignored(self) -> None:
        mascot = Mascot(40)
        mascot.set_size(0)
        self.assertEqual(mascot.width(), 40)


class UnknownStateTests(unittest.TestCase):
    def test_an_unknown_state_is_ignored_rather_than_drawn(self) -> None:
        mascot = Mascot(40)
        mascot.set_state(MascotState.READING)
        mascot.set_state("interpretive-dance")
        self.assertEqual(mascot.state(), MascotState.READING)


if __name__ == "__main__":
    unittest.main()


class VariantTests(unittest.TestCase):
    """Two crops of one character, with the old naming still working."""

    def test_a_state_prefers_the_crop_it_was_asked_for(self) -> None:
        with _Assets() as assets:
            assets.write(assets.final, "working-panel.svg")
            assets.write(assets.final, "working-full.svg")
            self.assertTrue(
                asset_for("working", Variant.FULL).endswith("working-full.svg"))
            self.assertTrue(
                asset_for("working", Variant.PANEL).endswith("working-panel.svg"))

    def test_the_old_plain_names_still_work(self) -> None:
        """Backwards compatibility: a set with no -full/-panel suffixes."""
        with _Assets() as assets:
            for state in ALL_STATES:
                assets.write(assets.final, f"{state}.svg")
            for variant in VARIANTS:
                self.assertTrue(
                    asset_for("thinking", variant).endswith("thinking.svg"),
                    f"the plain filename stopped working for {variant}")

    def test_this_state_uncropped_beats_idle_in_the_right_crop(self) -> None:
        # A drawing of the right moment matters more than a drawing of the
        # right shape, so "working.svg" wins over "idle-panel.svg".
        with _Assets() as assets:
            assets.write(assets.final, "working.svg")
            assets.write(assets.final, "idle-panel.svg")
            self.assertTrue(
                asset_for("working", Variant.PANEL).endswith("working.svg"))

    def test_a_missing_crop_falls_back_to_idle_in_that_crop(self) -> None:
        with _Assets() as assets:
            assets.write(assets.final, "idle-full.svg")
            self.assertTrue(
                asset_for("approval", Variant.FULL).endswith("idle-full.svg"))

    def test_one_final_file_outranks_the_whole_placeholder_set(self) -> None:
        with _Assets() as assets:
            for state in ALL_STATES:
                for variant in VARIANTS:
                    assets.write(assets.placeholder, f"{state}-{variant}.svg")
            assets.write(assets.final, "idle-full.svg")
            self.assertEqual(os.path.dirname(asset_for("complete", Variant.FULL)),
                             assets.final)
            self.assertTrue(has_final_artwork())

    def test_an_unknown_variant_is_treated_as_the_panel_crop(self) -> None:
        self.assertEqual(asset_for("idle", "sideways"), asset_for("idle", Variant.PANEL))

    def test_the_widget_keeps_a_tall_box(self) -> None:
        full = Mascot(120, variant=Variant.FULL, height=168)
        self.assertEqual((full.width(), full.height()), (120, 168))
        for state in ALL_STATES:
            full.set_state(state)
            self.assertFalse(full.pixmap().isNull(), state)

    def test_resizing_can_change_both_dimensions(self) -> None:
        full = Mascot(100, variant=Variant.FULL, height=140)
        full.set_size(80, 112)
        self.assertEqual((full.width(), full.height()), (80, 112))

    def test_the_new_tab_page_uses_the_full_crop(self) -> None:
        import base64

        from app.browser.newtab import NewTabData, render

        html = render(NewTabData())
        payload = html.split("base64,", 1)[1].split('"', 1)[0]
        drawing = base64.b64decode(payload).decode("utf-8")
        # Compared against the file itself rather than against a viewBox the
        # artwork happens to have today: the whole point of the drop-in
        # contract is that the final drawing may be any size it likes.
        with open(asset_for(MascotState.IDLE, Variant.FULL), encoding="utf-8") as handle:
            self.assertEqual(drawing, handle.read(),
                             "the new tab page is not using the full-body crop")
        self.assertNotEqual(asset_for(MascotState.IDLE, Variant.FULL),
                            asset_for(MascotState.IDLE, Variant.PANEL))


class ArtworkFirstMotionTests(unittest.TestCase):
    """Real artwork keeps its own expression."""

    def test_the_placeholder_is_allowed_to_be_warped(self) -> None:
        mascot = Mascot(40)
        mascot.set_state(MascotState.THINKING)
        mascot._blinking = 2
        self.assertFalse(has_final_artwork())
        self.assertFalse(mascot._animated_frame(mascot._still).isNull())

    def test_real_artwork_is_never_squashed_or_leaned(self) -> None:
        """A squash reads as a blink on flat shapes and as a fault on a drawing.

        Checked by comparing frames: with real artwork installed, the only
        motion left is a translation, so a frame with blinking forced on must
        be identical to one without it.
        """
        with _Assets() as assets:
            for variant in VARIANTS:
                assets.write(assets.final, f"idle-{variant}.svg")
            self.assertTrue(has_final_artwork())
            mascot = Mascot(40)
            mascot.set_state(MascotState.THINKING)
            mascot._elapsed = 0
            plain = mascot._animated_frame(mascot._still).toImage()
            mascot._blinking = 2
            mascot._elapsed = 0
            blinking = mascot._animated_frame(mascot._still).toImage()
            self.assertEqual(plain, blinking,
                             "the synthetic blink was applied to real artwork")

    @unittest.skipIf(reduced_motion(), "the machine asked for no animation")
    def test_real_artwork_still_breathes(self) -> None:
        # Translation cannot distort a drawing, so it stays.
        with _Assets() as assets:
            for variant in VARIANTS:
                assets.write(assets.final, f"idle-{variant}.svg")
            mascot = Mascot(40)
            self.assertTrue(mascot._frames.isActive())
