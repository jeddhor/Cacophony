"""The Prompt Compiler and structured output (design document sections 12, 13)."""

from __future__ import annotations

import json

import pytest

from cacophony.generation.prompt import PROMPT_VERSION, PromptCompiler, json_schema_for_fields
from cacophony.generation.structured import (
    StructuredOutputError,
    extract_json,
    parse_record,
    parse_records,
)
from helpers import compile_from, make_project

SCHEMA = {
    "ticket": {
        "count": 5,
        "description": "One helpdesk ticket.",
        "fields": {
            "category": {"type": "enum", "generator": "weighted", "choices": ["VPN", "Email"]},
            "device_type": {"generator": "weighted", "choices": ["laptop", "phone"]},
            "summary": {
                "type": "string",
                "semantic": "A one-line subject written by an employee.",
                "tone": "Informal professional",
                "generator": "llm",
                "context": ["category", "device_type"],
                "constraints": {"min_length": 12, "max_length": 90},
            },
            "severity_score": {
                "type": "integer",
                "semantic": "How urgent this is",
                "generator": "llm",
                "constraints": {"min": 1, "max": 5},
            },
            "labels": {
                "type": "array",
                "semantic": "Short routing labels",
                "generator": "llm",
                "constraints": {"min_length": 1, "max_length": 3},
            },
        },
    }
}


@pytest.fixture
def compiled():
    return compile_from(SCHEMA)


@pytest.fixture
def ai_fields(compiled):
    entity = compiled.entity("ticket")
    return [entity.field(name) for name in ("summary", "severity_score", "labels")]


class TestJsonSchema:
    def test_types_map_to_json_schema(self, ai_fields) -> None:
        schema = json_schema_for_fields(ai_fields)
        properties = schema["properties"]
        assert properties["summary"]["type"] == "string"
        assert properties["severity_score"]["type"] == "integer"
        assert properties["labels"]["type"] == "array"
        assert schema["additionalProperties"] is False

    def test_constraints_reach_the_schema(self, ai_fields) -> None:
        """Section 13: use JSON Schema internally where practical."""
        properties = json_schema_for_fields(ai_fields)["properties"]
        assert properties["summary"]["minLength"] == 12
        assert properties["summary"]["maxLength"] == 90
        assert properties["severity_score"]["minimum"] == 1
        assert properties["severity_score"]["maximum"] == 5
        assert properties["labels"]["minItems"] == 1

    def test_semantics_become_descriptions(self, ai_fields) -> None:
        properties = json_schema_for_fields(ai_fields)["properties"]
        assert "one-line subject" in properties["summary"]["description"]

    def test_required_excludes_nullable_fields(self) -> None:
        compiled = compile_from(
            {
                "e": {
                    "fields": {
                        "a": {"type": "text", "semantic": "x", "generator": "llm"},
                        "b": {
                            "type": "text",
                            "semantic": "y",
                            "generator": "llm",
                            "nullable": True,
                            "null_probability": 0.4,
                        },
                    }
                }
            }
        )
        entity = compiled.entity("e")
        schema = json_schema_for_fields([entity.field("a"), entity.field("b")])
        assert schema["required"] == ["a"]
        assert schema["properties"]["b"]["type"] == ["string", "null"]

    def test_enum_constraints_become_enums(self) -> None:
        compiled = compile_from(
            {
                "e": {
                    "fields": {
                        "mood": {
                            "type": "string",
                            "semantic": "the mood",
                            "generator": "llm",
                            "constraints": {"enum": ["calm", "urgent"]},
                        }
                    }
                }
            }
        )
        schema = json_schema_for_fields([compiled.entity("e").field("mood")])
        assert schema["properties"]["mood"]["enum"] == ["calm", "urgent"]


class TestPromptCompiler:
    def _compile(self, compiled, ai_fields, **kwargs):
        return PromptCompiler(compiled.spec).compile(
            compiled.entity("ticket").spec, ai_fields, **kwargs
        )

    def test_section_12_prompt_elements(self, compiled, ai_fields) -> None:
        """Section 12's list: descriptions, types, constraints, tone, context."""
        prompt = self._compile(compiled, ai_fields, context_fields=("category", "device_type"))
        text = prompt.instruction
        assert "one fictional ticket record" in text
        assert "One helpdesk ticket." in text  # entity description
        assert "A one-line subject written by an employee." in text  # semantics
        assert "Informal professional" in text  # tone
        assert "length: 12-90 characters" in text  # constraints
        assert "range: 1 to 5" in text
        assert '"category"' in text and '"device_type"' in text  # known context
        assert "STRICT JSON" in text

    def test_no_prompt_engineering_is_required_of_the_user(self, compiled, ai_fields) -> None:
        """Nothing in the schema said how to phrase anything (section 9)."""
        source = json.dumps(SCHEMA)
        assert "STRICT JSON" not in source
        assert "You generate synthetic test data" not in source
        prompt = self._compile(compiled, ai_fields)
        assert "STRICT JSON" in prompt.instruction
        assert "You generate synthetic test data" in prompt.system

    def test_system_prompt_forbids_real_identities(self, compiled, ai_fields) -> None:
        """Section 61: models may reproduce real information they memorised."""
        prompt = self._compile(compiled, ai_fields)
        assert "fictional" in prompt.system
        assert "example.com" in prompt.instruction

    def test_batch_prompt_asks_for_a_wrapped_array(self, compiled, ai_fields) -> None:
        prompt = self._compile(compiled, ai_fields, mode="batch", batch_size=7)
        assert "Generate 7 fictional ticket records" in prompt.instruction
        assert '{"records": [ ... ]}' in prompt.instruction
        records = prompt.json_schema["properties"]["records"]
        assert records["minItems"] == 7 and records["maxItems"] == 7

    def test_batch_prompt_asks_for_variety(self, compiled, ai_fields) -> None:
        """Section 59: language models repeat themselves."""
        prompt = self._compile(compiled, ai_fields, mode="batch", batch_size=5)
        assert "Vary the records" in prompt.instruction

    def test_known_values_are_rendered_per_record(self, compiled, ai_fields) -> None:
        prompt = self._compile(compiled, ai_fields, context_fields=("category",))
        rendered = prompt.render({"category": "VPN"})
        assert "Known values:" in rendered
        assert '"VPN"' in rendered

    def test_known_values_are_rendered_per_batch(self, compiled, ai_fields) -> None:
        prompt = self._compile(compiled, ai_fields, mode="batch", batch_size=2)
        rendered = prompt.render([{"category": "VPN"}, {"category": "Email"}])
        assert "Record 1:" in rendered and "Record 2:" in rendered

    def test_hash_is_stable_and_content_sensitive(self, compiled, ai_fields) -> None:
        """Section 76: the cache key must change when the prompt does."""
        first = self._compile(compiled, ai_fields)
        assert first.hash == self._compile(compiled, ai_fields).hash
        batched = self._compile(compiled, ai_fields, mode="batch", batch_size=4)
        assert batched.hash != first.hash

    def test_version_is_recorded(self, compiled, ai_fields) -> None:
        assert self._compile(compiled, ai_fields).version == PROMPT_VERSION

    def test_locale_reaches_the_system_prompt(self, ai_fields) -> None:
        project = make_project(SCHEMA, locale="fr_FR")
        compiled_fr = compile_from(SCHEMA, locale="fr_FR")
        prompt = PromptCompiler(project).compile(compiled_fr.entity("ticket").spec, ai_fields)
        assert "fr_FR" in prompt.system

    def test_profile_changes_the_system_prompt(self, ai_fields) -> None:
        for profile, marker in (
            ("high_realism", "specific, plausible detail"),
            ("quick_mock", "short and plain"),
        ):
            project = make_project(SCHEMA, profile=profile)
            compiled_p = compile_from(SCHEMA, profile=profile)
            prompt = PromptCompiler(project).compile(compiled_p.entity("ticket").spec, ai_fields)
            assert marker in prompt.system

    def test_forbidden_values_become_a_requirement(self) -> None:
        compiled = compile_from(
            {
                "e": {
                    "fields": {
                        "note": {
                            "type": "text",
                            "semantic": "a note",
                            "generator": "llm",
                            "constraints": {"forbidden": ["TODO"]},
                        }
                    }
                }
            }
        )
        prompt = PromptCompiler(compiled.spec).compile(
            compiled.entity("e").spec, [compiled.entity("e").field("note")]
        )
        assert "must never be any of" in prompt.instruction
        assert "TODO" in prompt.instruction


# --------------------------------------------------------------------------- #
# Structured output (section 13)
# --------------------------------------------------------------------------- #


class TestExtraction:
    def test_plain_json(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_block(self) -> None:
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_unlabelled_fence(self) -> None:
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_preamble_is_ignored(self) -> None:
        assert extract_json('Sure! Here it is:\n{"a": 1}') == {"a": 1}

    def test_trailing_commentary_is_ignored(self) -> None:
        assert extract_json('{"a": 1}\n\nLet me know if you need more.') == {"a": 1}

    def test_trailing_comma_is_repaired(self) -> None:
        assert extract_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}

    def test_truncated_output_is_repaired(self) -> None:
        """The classic shape of a response cut off by a token limit."""
        assert extract_json('{"a": 1, "b": "unterminated') == {"a": 1, "b": "unterminated"}

    def test_braces_inside_strings_do_not_confuse_the_scanner(self) -> None:
        assert extract_json('{"a": "a } brace"}') == {"a": "a } brace"}

    def test_arrays(self) -> None:
        assert extract_json("[1, 2, 3]") == [1, 2, 3]

    def test_empty_response(self) -> None:
        with pytest.raises(StructuredOutputError, match="empty"):
            extract_json("   ")

    def test_hopeless_output(self) -> None:
        with pytest.raises(StructuredOutputError, match="not valid JSON"):
            extract_json("I am afraid I cannot do that.")


class TestParsing:
    def test_valid_record(self, ai_fields) -> None:
        text = json.dumps(
            {"summary": "VPN will not connect from home", "severity_score": 3, "labels": ["vpn"]}
        )
        parsed = parse_record(text, ai_fields)
        assert parsed.ok
        assert parsed.values["severity_score"] == 3

    def test_missing_required_field_is_reported(self, ai_fields) -> None:
        parsed = parse_record(json.dumps({"summary": "a" * 20}), ai_fields)
        assert not parsed.ok
        assert "omitted required field" in parsed.problems

    def test_invented_field_is_a_warning_not_an_error(self, ai_fields) -> None:
        text = json.dumps(
            {
                "summary": "VPN will not connect from home",
                "severity_score": 2,
                "labels": ["vpn"],
                "confidence": 0.9,
            }
        )
        parsed = parse_record(text, ai_fields)
        assert parsed.ok
        assert any("invented a field" in issue.message for issue in parsed.result.warnings)

    def test_case_insensitive_field_names(self, ai_fields) -> None:
        text = json.dumps(
            {"Summary": "VPN will not connect", "Severity_Score": 2, "LABELS": ["vpn"]}
        )
        parsed = parse_record(text, ai_fields)
        assert parsed.values["summary"] == "VPN will not connect"

    def test_string_numbers_are_coerced(self, ai_fields) -> None:
        text = json.dumps({"summary": "a" * 20, "severity_score": "4", "labels": ["x"]})
        parsed = parse_record(text, ai_fields)
        assert parsed.values["severity_score"] == 4

    def test_overlong_string_is_truncated_at_a_boundary(self, ai_fields) -> None:
        long = "The VPN client refuses to connect from home. " * 6
        parsed = parse_record(
            json.dumps({"summary": long, "severity_score": 1, "labels": ["x"]}), ai_fields
        )
        assert len(parsed.values["summary"]) <= 90
        assert any("truncated" in repair for repair in parsed.repairs)

    def test_out_of_range_number_is_clamped(self, ai_fields) -> None:
        parsed = parse_record(
            json.dumps({"summary": "a" * 20, "severity_score": 99, "labels": ["x"]}), ai_fields
        )
        assert parsed.values["severity_score"] == 5
        assert any("clamped" in repair for repair in parsed.repairs)

    def test_surrounding_quotes_are_stripped(self, ai_fields) -> None:
        parsed = parse_record(
            json.dumps({"summary": '"VPN will not connect"', "severity_score": 1, "labels": ["x"]}),
            ai_fields,
        )
        assert not parsed.values["summary"].startswith('"')

    def test_a_single_object_in_a_list_is_unwrapped(self, ai_fields) -> None:
        text = json.dumps([{"summary": "a" * 20, "severity_score": 1, "labels": ["x"]}])
        assert parse_record(text, ai_fields).ok

    def test_non_object_payload_is_rejected(self, ai_fields) -> None:
        with pytest.raises(StructuredOutputError, match="expected a JSON object"):
            parse_record("42", ai_fields)


class TestBatchParsing:
    def _record(self, index: int) -> dict:
        return {
            "summary": f"Issue number {index} with the network",
            "severity_score": 2,
            "labels": ["net"],
        }

    def test_wrapped_records_key(self, ai_fields) -> None:
        text = json.dumps({"records": [self._record(i) for i in range(3)]})
        assert len(parse_records(text, ai_fields, expected=3)) == 3

    def test_other_wrapper_keys_are_accepted(self, ai_fields) -> None:
        for key in ("data", "items", "results", "rows"):
            text = json.dumps({key: [self._record(1)]})
            assert len(parse_records(text, ai_fields, expected=1)) == 1

    def test_bare_array(self, ai_fields) -> None:
        text = json.dumps([self._record(i) for i in range(2)])
        assert len(parse_records(text, ai_fields, expected=2)) == 2

    def test_single_object_for_a_batch_of_one(self, ai_fields) -> None:
        assert len(parse_records(json.dumps(self._record(1)), ai_fields, expected=1)) == 1

    def test_surplus_records_are_discarded(self, ai_fields) -> None:
        text = json.dumps({"records": [self._record(i) for i in range(9)]})
        assert len(parse_records(text, ai_fields, expected=4)) == 4

    def test_short_batch_returns_what_arrived(self, ai_fields) -> None:
        """The caller decides whether to retry; good records are not thrown away."""
        text = json.dumps({"records": [self._record(1)]})
        assert len(parse_records(text, ai_fields, expected=5)) == 1

    def test_empty_batch_is_an_error(self, ai_fields) -> None:
        with pytest.raises(StructuredOutputError, match="no usable records"):
            parse_records(json.dumps({"records": []}), ai_fields, expected=3)
