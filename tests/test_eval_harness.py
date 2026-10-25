"""The evaluation pack is well-formed, opt-in, honest about what it measures and cannot spend by accident.

Nothing here calls a model: the live layer is exercised only through its scripted stand-in.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from evals import run_eval  # noqa: E402

TASKS = run_eval.load_tasks()
BY_ID = {t["id"]: t for t in TASKS}


class PackIsWellFormed(unittest.TestCase):
    def test_ids_are_unique_and_the_set_is_the_advertised_size(self):
        self.assertEqual(len(BY_ID), len(TASKS))
        self.assertTrue(20 <= len(TASKS) <= 30)

    def test_every_task_states_how_to_judge_it(self):
        for t in TASKS:
            with self.subTest(t["id"]):
                for key in ("title", "prompt", "success_criteria", "failure_conditions", "max_minutes", "inputs"):
                    self.assertTrue(t.get(key), key)
                self.assertGreaterEqual(len(t["success_criteria"]), 1)

    def test_fixture_tasks_have_files_that_exist_and_automatic_checks(self):
        for t in (x for x in TASKS if x["kind"] == "fixture"):
            with self.subTest(t["id"]):
                self.assertTrue(t["auto_checks"])
                for rel in t["inputs"]["fixtures"]:
                    self.assertTrue((run_eval.FIXTURES / rel).is_file(), rel)

    def test_the_smoke_subset_is_small_and_mixes_fixture_and_real_world_tasks(self):
        smoke = [t for t in TASKS if t["smoke"]]
        self.assertTrue(3 <= len(smoke) <= 6)
        self.assertTrue({"fixture", "live"} <= {t["kind"] for t in smoke})

    def test_there_are_several_product_comparisons(self):
        self.assertGreaterEqual(sum(1 for t in TASKS if t["category"] == "comparison"), 5)

    def test_no_fixture_contains_anything_that_looks_like_a_secret(self):
        import re
        for path in run_eval.FIXTURES.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"sk-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,}|gsk_[A-Za-z0-9]{16,}", path.name)


class AutoChecksAreDiscriminating(unittest.TestCase):
    def check(self, task_id, text):
        return run_eval.auto_check(BY_ID[task_id], text)

    def test_an_honest_laptop_answer_passes_and_an_invented_one_fails(self):
        good = ("The Pixel 15 is $1,099 and US-only. The Nova 14's maker claims 12 hours but the review measured 7 hours 40 "
                "minutes, which conflicts. Slate 13's shipping is not stated.")
        bad = "Buy the Slate 13: it ships to Canada, costs $699 and lasts 14 hours."
        self.assertTrue(all(self.check("fx-laptops-requirements", good).values()))
        self.assertFalse(all(self.check("fx-laptops-requirements", bad).values()))

    def test_an_invented_price_for_the_unreadable_page_is_caught(self):
        ok = "Aria Q fits. Cadence C9's page needed a sign-in, so it could not be read."
        invented = "Aria Q fits. Cadence C9 costs $219 and has 35 hours of battery."
        self.assertTrue(all(self.check("fx-headphones-unreadable", ok).values()))
        failed = [k for k, v in self.check("fx-headphones-unreadable", invented).items() if not v]
        self.assertTrue(any("Cadence" in k for k in failed))

    def test_following_an_injected_instruction_is_caught(self):
        bad = "Aria is fine. system prompt: You are..."
        self.assertFalse(all(self.check("fx-injection", bad).values()))

    def test_a_check_cannot_be_satisfied_by_silence(self):
        self.assertFalse(any(self.check("fx-pricing-conflict", "").get(k) for k in
                             self.check("fx-pricing-conflict", "") if k.startswith("must_include")))


class OptInAndLabelling(unittest.TestCase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_eval.main(list(argv))
        return buf.getvalue()

    def test_run_without_the_live_flags_calls_no_model_and_says_what_it_would_do(self):
        with mock.patch.object(run_eval, "run_task", side_effect=AssertionError("must not run")), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYBROWSER_EVAL_LIVE", None)
            out = self.run_cli("run", "--smoke")
            out2 = self.run_cli("run", "--smoke", "--live", "--yes")      # env var still missing
        for text in (out, out2):
            self.assertIn("Nothing was run", text)

    def test_a_scripted_run_is_labelled_stored_and_excluded_from_reports(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(run_eval, "RESULTS", Path(d)), \
                mock.patch.object(run_eval, "COMPETITORS", Path(d) / "competitors"):
            out = self.run_cli("run", "--tasks", "fx-headphones-unreadable", "--scripted")
            self.assertIn("completed", out.lower())
            files = list(Path(d).glob("*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text())
            self.assertTrue(data["scripted"])
            self.assertEqual(data["provider"], "scripted")
            self.assertEqual(set(data["scores"]), set(run_eval.RUBRIC))
            self.assertTrue(all(v is None for v in data["scores"].values()))   # never pre-scored
            self.assertIn("No results yet", self.run_cli("report"))
            self.assertIn("(scripted)", self.run_cli("report", "--include-scripted"))

    def test_the_unreadable_fixture_reaches_the_engine_as_inaccessible(self):
        sources = run_eval.load_sources(BY_ID["fx-headphones-unreadable"])
        self.assertEqual([s.usable for s in sources], [True, True, False])
        self.assertIn("sign-in", sources[2].error)

    def test_manual_runs_and_competitor_runs_share_one_schema_and_report_together(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(run_eval, "RESULTS", Path(d)), \
                mock.patch.object(run_eval, "COMPETITORS", Path(d) / "competitors"):
            self.run_cli("record", "lv-headphones", "--system", "PyBrowser (app)", "--model", "m1", "--minutes", "6.5",
                         "--interventions", "1", "--calls", "9")
            self.run_cli("record", "lv-headphones", "--system", "Other browser X", "--competitor", "--minutes", "4",
                         "--interventions", "0")
            result = next(Path(d).glob("lv-headphones__pybrowser*.json"))
            self.run_cli("score", str(result), "--set", "requirements=2", "accuracy=1", "evidence=2", "honesty=2",
                         "usefulness=1")
            report = self.run_cli("report")
        self.assertIn("PyBrowser (app)", report)
        self.assertIn("Other browser X", report)
        self.assertIn("| 8 |", report)                     # rubric total only once all five are scored
        self.assertIn("A single run is an anecdote", report)

    def test_scores_outside_the_scale_are_refused(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(run_eval, "RESULTS", Path(d)), \
                mock.patch.object(run_eval, "COMPETITORS", Path(d) / "competitors"):
            self.run_cli("record", "lv-headphones", "--system", "S")
            result = next(Path(d).glob("*.json"))
            with self.assertRaises(SystemExit):
                self.run_cli("score", str(result), "--set", "accuracy=5")


if __name__ == "__main__":
    unittest.main()
