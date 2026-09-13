"""Skill model, built-ins, and output-schema validation - pure Python, no
Qt, no agent loop. See app/agent/skills.py.

Run with:
    python -m unittest tests.test_skills -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.skills import (  # noqa: E402
    BUILTIN_SKILLS,
    OutputValidationError,
    Skill,
    builtin_skill,
    validate_output,
)
from app.agent.tools import TOOL_NAMES  # noqa: E402


class BuiltinSkillTests(unittest.TestCase):
    def test_the_five_built_ins_exist(self) -> None:
        ids = {s.id for s in BUILTIN_SKILLS}
        self.assertEqual(ids, {"deep_research", "compare", "summarize", "plan", "audit"})

    def test_every_built_in_is_marked_builtin(self) -> None:
        for skill in BUILTIN_SKILLS:
            self.assertTrue(skill.builtin, skill.id)

    def test_every_built_ins_allowlist_only_names_real_tools(self) -> None:
        for skill in BUILTIN_SKILLS:
            if skill.allowed_tools is None:
                continue
            for name in skill.allowed_tools:
                self.assertIn(name, TOOL_NAMES, f"{skill.id} allows unknown tool {name!r}")

    def test_no_built_in_allows_a_destructive_or_form_submitting_tool(self) -> None:
        """None of the five need to click, submit, or check anything -
        they read, navigate, and record findings."""
        disallowed = {"browser_click", "browser_submit", "browser_select",
                     "browser_set_checked"}
        for skill in BUILTIN_SKILLS:
            if skill.allowed_tools is None:
                continue
            self.assertFalse(set(skill.allowed_tools) & disallowed, skill.id)

    def test_builtin_skill_looks_up_by_id(self) -> None:
        self.assertEqual(builtin_skill("summarize").name, "Summarize")
        self.assertIsNone(builtin_skill("does-not-exist"))

    def test_compare_and_summarize_declare_an_output_schema(self) -> None:
        self.assertIsNotNone(builtin_skill("compare").output_schema)
        self.assertIsNotNone(builtin_skill("summarize").output_schema)


class DuplicationTests(unittest.TestCase):
    def test_duplicating_a_builtin_produces_a_non_builtin_copy(self) -> None:
        original = builtin_skill("summarize")
        copy = original.duplicated(new_id="summarize-mine")
        self.assertFalse(copy.builtin)
        self.assertEqual(copy.id, "summarize-mine")
        self.assertIn("Copy", copy.name)
        self.assertEqual(copy.instructions, original.instructions)
        self.assertEqual(copy.allowed_tools, original.allowed_tools)

    def test_duplicating_does_not_mutate_the_original(self) -> None:
        original = builtin_skill("summarize")
        original.duplicated(new_id="x", name="Renamed")
        self.assertEqual(builtin_skill("summarize").name, "Summarize")
        self.assertTrue(builtin_skill("summarize").builtin)

    def test_a_custom_name_can_be_given(self) -> None:
        copy = builtin_skill("plan").duplicated(new_id="plan-2", name="My Plan Variant")
        self.assertEqual(copy.name, "My Plan Variant")


class SerializationTests(unittest.TestCase):
    def test_as_dict_and_from_dict_round_trip(self) -> None:
        skill = Skill(id="s1", name="S1", description="d", instructions="i",
                      allowed_tools=("browser_get_page_text",),
                      output_schema={"type": "object"},
                      preferred_provider="anthropic", preferred_model="claude-x",
                      default_context_kinds=("tab",))
        restored = Skill.from_dict(skill.as_dict())
        self.assertEqual(restored.name, skill.name)
        self.assertEqual(restored.allowed_tools, skill.allowed_tools)
        self.assertEqual(restored.output_schema, skill.output_schema)
        self.assertFalse(restored.builtin)

    def test_a_none_allowlist_round_trips_as_none(self) -> None:
        skill = Skill(id="s1", name="S1", description="", instructions="", allowed_tools=None)
        restored = Skill.from_dict(skill.as_dict())
        self.assertIsNone(restored.allowed_tools)


class OutputValidationTests(unittest.TestCase):
    def test_a_valid_json_object_matching_the_schema_passes(self) -> None:
        schema = {"type": "object", "required": ["summary"],
                 "properties": {"summary": {"type": "string"}}}
        result = validate_output(schema, '{"summary": "It is about cats."}')
        self.assertEqual(result["summary"], "It is about cats.")

    def test_a_missing_required_field_fails_cleanly(self) -> None:
        schema = {"type": "object", "required": ["summary"]}
        with self.assertRaises(OutputValidationError):
            validate_output(schema, '{"not_summary": "x"}')

    def test_non_json_text_fails_cleanly_not_with_a_crash(self) -> None:
        schema = {"type": "object", "required": ["summary"]}
        with self.assertRaises(OutputValidationError):
            validate_output(schema, "This is just a plain sentence, not JSON.")

    def test_json_embedded_in_a_sentence_is_recovered(self) -> None:
        schema = {"type": "object", "required": ["summary"]}
        text = 'Sure, here it is: {"summary": "ok"} - hope that helps!'
        result = validate_output(schema, text)
        self.assertEqual(result["summary"], "ok")

    def test_wrong_field_type_fails_cleanly(self) -> None:
        schema = {"type": "object", "properties": {"key_points": {"type": "array"}}}
        with self.assertRaises(OutputValidationError):
            validate_output(schema, '{"key_points": "not a list"}')

    def test_nested_array_items_are_checked(self) -> None:
        schema = {"type": "object", "properties": {
            "key_points": {"type": "array", "items": {"type": "string"}}}}
        with self.assertRaises(OutputValidationError):
            validate_output(schema, '{"key_points": [1, 2, 3]}')

    def test_a_valid_nested_structure_passes(self) -> None:
        schema = {"type": "object", "required": ["summary", "key_points"],
                 "properties": {"summary": {"type": "string"},
                               "key_points": {"type": "array", "items": {"type": "string"}}}}
        result = validate_output(
            schema, '{"summary": "ok", "key_points": ["a", "b"]}')
        self.assertEqual(result["key_points"], ["a", "b"])

    def test_enum_violation_fails_cleanly(self) -> None:
        schema = {"type": "object", "properties": {"status": {"enum": ["ok", "bad"]}}}
        with self.assertRaises(OutputValidationError):
            validate_output(schema, '{"status": "maybe"}')


if __name__ == "__main__":
    unittest.main()
