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

    def test_artwork_exists_for_every_state_and_crop(self) -> None:
        # Deliberately indifferent to the extension: the drop-in contract
        # promises any of gif/webp/apng/png/svg will do, and the artwork went
        # from SVG to PNG without a line of code changing. What must hold is
        # that each state and crop has its OWN file rather than a fallback.
        for state in ALL_STATES:
            for variant in VARIANTS:
                path = asset_for(state, variant)
                self.assertIsNotNone(path, f"nothing to draw for {state}/{variant}")
                stem = os.path.splitext(os.path.basename(path))[0]
                self.assertEqual(stem, f"{state}-{variant}",
                                 f"{state}/{variant} fell back unexpectedly")

    def test_the_two_crops_are_different_drawings(self) -> None:
        # A bust is not a full body scaled down - cramming the whole figure
        # into a 40px slot is what this variant system exists to avoid.
        for state in ALL_STATES:
            full = asset_for(state, Variant.FULL)
            panel = asset_for(state, Variant.PANEL)
            self.assertNotEqual(full, panel, state)

    def test_the_supplied_artwork_outranks_the_stand_in(self) -> None:
        # The real Py lives in assets/mascot/ and the stand-in one level down,
        # so the two can never be confused for each other. Now that real
        # artwork is installed, nothing may resolve to the stand-in.
        self.assertTrue(has_final_artwork(),
                        "the supplied artwork is not being seen as real")
        for state in ALL_STATES:
            for variant in VARIANTS:
                self.assertNotIn("placeholder", asset_for(state, variant),
                                 f"{state}/{variant} is still on the stand-in")

    def test_a_stand_in_on_its_own_is_never_called_real(self) -> None:
        # The other half of that separation, which the shipped state can no
        # longer demonstrate: placeholders alone must still report False.
        with _Assets() as assets:
            for state in ALL_STATES:
                assets.write(assets.placeholder, f"{state}-panel.svg")
            self.assertFalse(has_final_artwork(),
                             "a placeholder is being reported as the real artwork")

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
        drawing = base64.b64decode(payload)
        # Compared byte for byte against the file itself, not against anything
        # the artwork happens to look like today: the drop-in contract promises
        # the final drawing may be any format and any size. Bytes, not text -
        # the artwork is a PNG.
        with open(asset_for(MascotState.IDLE, Variant.FULL), "rb") as handle:
            self.assertEqual(drawing, handle.read(),
                             "the new tab page is not using the full-body crop")
        self.assertNotEqual(asset_for(MascotState.IDLE, Variant.FULL),
                            asset_for(MascotState.IDLE, Variant.PANEL))


class ArtworkFirstMotionTests(unittest.TestCase):
    """Real artwork keeps its own expression."""

    def test_the_placeholder_is_allowed_to_be_warped(self) -> None:
        # Against its own folder rather than against whatever the repository
        # happens to ship: this is a claim about placeholders, and it used to
        # pass only because no real artwork was installed yet.
        with _Assets() as assets:
            for variant in VARIANTS:
                assets.write(assets.placeholder, f"idle-{variant}.svg")
            self.assertFalse(has_final_artwork())
            mascot = Mascot(40)
            mascot.set_state(MascotState.THINKING)
            mascot._blinking = 2
            self.assertFalse(mascot._animated_frame(mascot._still).isNull())

    def test_real_artwork_is_never_squashed(self) -> None:
        """A squash reads as a blink on flat shapes and as a fault on a drawing.

        Checked by comparing frames: with real artwork installed the blink is
        off, so a frame with blinking forced on must be identical to one
        without it. Rotation and uniform scale are *not* covered by this - they
        are rigid, they cannot distort the drawing, and they are what keeps
        finished artwork from sitting there completely inert.
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


class LivelinessTests(unittest.TestCase):
    """Py should look alive without the drawing ever looking wrong."""

    def _with_artwork(self, assets) -> None:
        for variant in VARIANTS:
            assets.write(assets.final, f"idle-{variant}.svg")

    @unittest.skipIf(reduced_motion(), "the machine asked for no animation")
    def test_finished_artwork_still_moves(self) -> None:
        """The regression this guards: real artwork used to be frozen solid.

        Rotation and uniform scale were switched off along with the squash, on
        the grounds that all three "warp" the image. Two of them do not.
        """
        with _Assets() as assets:
            self._with_artwork(assets)
            mascot = Mascot(64)
            mascot.set_state(MascotState.WORKING)
            motion = mascot_module._MOTION[MascotState.WORKING]
            mascot._elapsed = 0
            still = mascot._animated_frame(mascot._still).toImage()
            mascot._elapsed = motion.period_ms // 4      # the crest of the breath
            moved = mascot._animated_frame(mascot._still).toImage()
            self.assertNotEqual(still, moved, "finished artwork never moves")

    @unittest.skipIf(reduced_motion(), "the machine asked for no animation")
    def test_working_reads_busier_than_idle(self) -> None:
        # Not decoration: working is the state a user glances at to decide
        # whether anything is happening, so its cadence has to be quicker than
        # a resting breath rather than merely different.
        working = mascot_module._MOTION[MascotState.WORKING]
        idle = mascot_module._MOTION[MascotState.IDLE]
        self.assertLess(working.period_ms, idle.period_ms)
        self.assertGreaterEqual(working.bob, idle.bob)

    def test_every_amplitude_stays_subtle(self) -> None:
        # The brief is "glance at Py and believe he is working", not "watch Py".
        # A pixel or so of travel and a degree or so of tilt is the whole budget.
        for state, motion in mascot_module._MOTION.items():
            self.assertLessEqual(motion.bob, 1.5, f"{state} bobs too far")
            self.assertLessEqual(abs(motion.lean), 2.0, f"{state} leans too far")
            self.assertLessEqual(motion.pulse, 0.03, f"{state} pulses too hard")
            self.assertLessEqual(motion.entry_pop, 0.08, f"{state} pops too hard")

    @unittest.skipIf(reduced_motion(), "the machine asked for no animation")
    def test_complete_celebrates_on_arrival_and_then_settles(self) -> None:
        """A celebration that loops forever stops being a celebration."""
        motion = mascot_module._MOTION[MascotState.COMPLETE]
        self.assertTrue(motion.entry_ms, "complete has no arrival animation")
        mascot = Mascot(64)
        mascot.set_state(MascotState.COMPLETE)
        self.assertEqual(mascot._elapsed, 0, "set_state did not restart the clock")
        peak = max(mascot._entry_curve_at(ms)
                   for ms in range(0, motion.entry_ms, 20))
        self.assertAlmostEqual(peak, 1.0, delta=0.06)
        mascot._elapsed = motion.entry_ms
        self.assertEqual(mascot._entry_curve(motion), 0.0,
                         "the celebration is still running after it should end")
        mascot._elapsed = motion.entry_ms * 40
        self.assertEqual(mascot._entry_curve(motion), 0.0)

    def test_no_other_state_pops_on_arrival(self) -> None:
        # An arrival animation on a state the agent passes through constantly
        # would make the panel twitch its way through every task.
        for state, motion in mascot_module._MOTION.items():
            if state != MascotState.COMPLETE:
                self.assertEqual(motion.entry_ms, 0, f"{state} pops on arrival")

    def test_reduced_motion_stops_the_arrival_animation_too(self) -> None:
        real = mascot_module.reduced_motion
        try:
            mascot_module.reduced_motion = lambda: True
            mascot = Mascot(64)
            mascot.set_state(MascotState.COMPLETE)
            mascot._elapsed = 200          # mid-celebration
            frame = mascot._animated_frame(mascot._still)
            self.assertIs(frame, mascot._still,
                          "the celebration ran with reduced motion on")
        finally:
            mascot_module.reduced_motion = real
