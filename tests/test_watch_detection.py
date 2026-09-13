"""Deterministic change detection - pure functions, no Qt/browser/network.

Run with:
    python -m unittest tests.test_watch_detection -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.watches.detection import (  # noqa: E402
    evaluate_check,
    extract_first_number,
    hash_text,
    looks_available,
    normalize_text,
)
from app.watches.model import WatchCondition  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_collapses_whitespace(self) -> None:
        self.assertEqual(normalize_text("a   b\n\tc"), "a b c")

    def test_strips_a_relative_timestamp(self) -> None:
        self.assertEqual(normalize_text("Updated 3 minutes ago. Price: $10"),
                         "Updated . Price: $10")

    def test_strips_a_clock_time(self) -> None:
        self.assertIn("Posted at", normalize_text("Posted at 10:42 PM today"))
        self.assertNotIn("10:42", normalize_text("Posted at 10:42 PM today"))


class ExtractNumberTests(unittest.TestCase):
    def test_a_plain_price(self) -> None:
        self.assertEqual(extract_first_number("Price: $1,299.00"), 1299.0)

    def test_a_percentage(self) -> None:
        self.assertEqual(extract_first_number("Battery: 42%"), 42.0)

    def test_no_number_returns_none(self) -> None:
        self.assertIsNone(extract_first_number("no numbers here"))


class AvailabilityTests(unittest.TestCase):
    def test_out_of_stock_phrase(self) -> None:
        self.assertFalse(looks_available("Sold Out - notify me"))

    def test_add_to_cart_phrase(self) -> None:
        self.assertTrue(looks_available("In stock. Add to Cart"))

    def test_neither_phrase_is_unknown(self) -> None:
        self.assertIsNone(looks_available("A page about widgets"))


class EvaluateCheckTests(unittest.TestCase):
    def test_the_first_check_only_establishes_a_baseline(self) -> None:
        result = evaluate_check(WatchCondition.ANY_CHANGE, "", "hello world",
                                previous_hash=None, previous_value=None)
        self.assertFalse(result.changed)
        self.assertFalse(result.meaningful)

    def test_identical_content_is_not_a_change(self) -> None:
        h = hash_text(normalize_text("hello world"))
        result = evaluate_check(WatchCondition.ANY_CHANGE, "", "hello   world",
                                previous_hash=h, previous_value=None)
        self.assertFalse(result.changed)
        self.assertFalse(result.meaningful)

    def test_any_change_alerts_once_content_actually_differs(self) -> None:
        h = hash_text(normalize_text("hello world"))
        result = evaluate_check(WatchCondition.ANY_CHANGE, "", "goodbye world",
                                previous_hash=h, previous_value=None)
        self.assertTrue(result.changed)
        self.assertTrue(result.meaningful)
        self.assertIn("goodbye world", result.summary)

    def test_a_pure_timestamp_change_is_not_reported_as_a_page_change(self) -> None:
        baseline = normalize_text("Last updated 2 minutes ago. Price: $10")
        h = hash_text(baseline)
        result = evaluate_check(WatchCondition.ANY_CHANGE, "", "Last updated 5 minutes ago. Price: $10",
                                previous_hash=h, previous_value=None)
        self.assertFalse(result.changed)

    def test_value_below_does_not_fire_until_the_price_actually_crosses(self) -> None:
        h = hash_text(normalize_text("Price: $100"))
        # Still above the threshold - no alert.
        result = evaluate_check(WatchCondition.VALUE_BELOW, "50", "Price: $90",
                                previous_hash=h, previous_value="100.0")
        self.assertTrue(result.changed)
        self.assertFalse(result.meaningful)

    def test_value_below_fires_on_the_crossing(self) -> None:
        h = hash_text(normalize_text("Price: $60"))
        result = evaluate_check(WatchCondition.VALUE_BELOW, "50", "Price: $45",
                                previous_hash=h, previous_value="60.0")
        self.assertTrue(result.meaningful)
        self.assertIn("45", result.summary)

    def test_value_below_does_not_refire_while_still_under_the_threshold(self) -> None:
        """Repeated identical (already-crossed) state must not alert again -
        the required 'no repeat alert' behaviour."""
        h = hash_text(normalize_text("Price: $45, free shipping"))
        result = evaluate_check(WatchCondition.VALUE_BELOW, "50", "Price: $44, free shipping",
                                previous_hash=h, previous_value="45.0")
        self.assertTrue(result.changed)   # the text did change (shipping line, price)
        self.assertFalse(result.meaningful)  # but it was already below the threshold

    def test_value_above_fires_on_the_crossing(self) -> None:
        h = hash_text(normalize_text("Score: 40"))
        result = evaluate_check(WatchCondition.VALUE_ABOVE, "50", "Score: 60",
                                previous_hash=h, previous_value="40.0")
        self.assertTrue(result.meaningful)

    def test_a_numeric_condition_never_guesses_when_no_number_is_present(self) -> None:
        h = hash_text(normalize_text("Price: $100"))
        result = evaluate_check(WatchCondition.VALUE_BELOW, "50", "Sorry, page not found",
                                previous_hash=h, previous_value="100.0")
        self.assertTrue(result.changed)
        self.assertFalse(result.meaningful)

    def test_text_contains_fires_only_on_the_transition(self) -> None:
        h = hash_text(normalize_text("Status: pending"))
        result = evaluate_check(WatchCondition.TEXT_CONTAINS, "shipped", "Status: shipped",
                                previous_hash=h, previous_value="false")
        self.assertTrue(result.meaningful)
        self.assertIn("shipped", result.summary)

    def test_text_contains_does_not_refire_once_already_true(self) -> None:
        h = hash_text(normalize_text("Status: shipped today"))
        result = evaluate_check(WatchCondition.TEXT_CONTAINS, "shipped", "Status: shipped yesterday",
                                previous_hash=h, previous_value="true")
        self.assertTrue(result.changed)
        self.assertFalse(result.meaningful)

    def test_text_not_contains_fires_when_the_phrase_disappears(self) -> None:
        h = hash_text(normalize_text("Status: temporarily unavailable"))
        result = evaluate_check(WatchCondition.TEXT_NOT_CONTAINS, "unavailable", "Status: ready to ship",
                                previous_hash=h, previous_value="true")
        self.assertTrue(result.meaningful)

    def test_becomes_available_fires_on_the_transition(self) -> None:
        h = hash_text(normalize_text("Sold Out"))
        result = evaluate_check(WatchCondition.BECOMES_AVAILABLE, "", "In stock, Add to Cart",
                                previous_hash=h, previous_value="false")
        self.assertTrue(result.meaningful)

    def test_becomes_available_does_not_refire_while_still_available(self) -> None:
        h = hash_text(normalize_text("In stock, Add to Cart, 5 left"))
        result = evaluate_check(WatchCondition.BECOMES_AVAILABLE, "", "In stock, Add to Cart, 3 left",
                                previous_hash=h, previous_value="true")
        self.assertTrue(result.changed)
        self.assertFalse(result.meaningful)


if __name__ == "__main__":
    unittest.main()
