"""AI-assisted schema creation (design document section 50).

The assistant's contract is that nothing reaches the user untested: a proposal
is translated, compiled and linted before it is shown, and a proposal that
cannot survive that is repaired or refused. So most of these tests feed it
answers a real model would plausibly give - including bad ones - and check
that what comes out the other end is a schema that works.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from cacophony.core.types import DataType
from cacophony.generation.engine import GenerationEngine
from cacophony.providers.registry import PROVIDER_REGISTRY
from cacophony.schema.assistant import (
    SchemaAssistant,
    SchemaProposalError,
    proposal_json_schema,
    to_project_data,
    to_yaml,
)
from cacophony.schema.compiler import compile_project
from cacophony.schema.loader import load_project_data

GOOD_ANSWER: dict[str, Any] = {
    "name": "Corporate Security",
    "description": "Employees, laptops and login activity",
    "entities": [
        {
            "name": "Department",
            "count": 14,
            "fields": [
                {"name": "department_id", "type": "integer", "primary_key": True},
                {"name": "name", "type": "string", "semantic": "The department's name"},
            ],
        },
        {
            "name": "employee",
            "count": 5000,
            "description": "A person employed by the company",
            "fields": [
                {"name": "employee_id", "type": "integer", "primary_key": True},
                {"name": "full_name", "type": "string", "semantic": "Person's full name"},
                {"name": "department", "type": "integer", "references": "Department"},
                {
                    "name": "status",
                    "type": "enum",
                    "choices": ["active", "on_leave", "departed"],
                    "semantic": "Employment status",
                },
            ],
        },
        {
            "name": "login_event",
            "count": 900000,
            "fields": [
                {"name": "event_id", "type": "integer", "primary_key": True},
                {"name": "employee", "type": "integer", "references": "employee"},
                {"name": "occurred_at", "type": "datetime", "semantic": "When it happened"},
            ],
        },
    ],
}


def assistant(*responses: str, **options: Any) -> SchemaAssistant:
    from cacophony.schema.models import ProviderSpec

    spec = ProviderSpec(
        id=f"assistant-{id(responses)}",
        type="language_model",
        adapter="mock",
        options={"responses": list(responses)},
    )
    return SchemaAssistant(PROVIDER_REGISTRY.create(spec), **options)  # type: ignore[arg-type]


def propose(*responses: str, **options: Any) -> Any:
    return asyncio.run(
        assistant(*responses, **options).propose("a company", **options.pop("ask", {}))
    )


class TestTranslation:
    def test_entity_and_field_names_are_normalised(self) -> None:
        data = to_project_data(GOOD_ANSWER)
        assert list(data["entities"]) == ["department", "employee", "login_event"]

    def test_a_reference_becomes_a_reference_generator(self) -> None:
        data = to_project_data(GOOD_ANSWER)
        field = data["entities"]["employee"]["fields"]["department"]
        assert field["generator"] == "reference"
        assert field["entity"] == "department"
        # Event data concentrates; uniform would be the tidier lie.
        assert field["distribution"] == "skewed"

    def test_a_reference_to_an_entity_that_was_not_proposed_is_dropped(self) -> None:
        answer = json.loads(json.dumps(GOOD_ANSWER))
        answer["entities"][1]["fields"].append(
            {"name": "ghost", "type": "string", "references": "nowhere", "semantic": "a ghost"}
        )
        data = to_project_data(answer)
        ghost = data["entities"]["employee"]["fields"]["ghost"]
        assert "generator" not in ghost
        assert ghost["semantic"] == "a ghost"

    def test_the_key_gets_a_generator_and_the_rest_get_a_meaning(self) -> None:
        """Cacophony picks generators; the model supplies meaning (section 68)."""
        data = to_project_data(GOOD_ANSWER)
        fields = data["entities"]["employee"]["fields"]
        assert fields["employee_id"]["generator"] == "sequence"
        assert "generator" not in fields["full_name"]
        assert fields["full_name"]["semantic"] == "Person's full name"

    def test_an_enum_becomes_a_weighted_choice(self) -> None:
        data = to_project_data(GOOD_ANSWER)
        status = data["entities"]["employee"]["fields"]["status"]
        assert status["generator"] == "weighted"
        assert status["choices"] == ["active", "on_leave", "departed"]

    def test_an_enum_with_no_choices_falls_back_to_a_string(self) -> None:
        answer = {
            "name": "x",
            "entities": [
                {
                    "name": "thing",
                    "count": 2,
                    "fields": [{"name": "kind", "type": "enum", "choices": []}],
                }
            ],
        }
        field = to_project_data(answer)["entities"]["thing"]["fields"]["kind"]
        assert field["type"] == "string"
        assert "choices" not in field

    def test_an_unknown_type_becomes_a_string_rather_than_an_error(self) -> None:
        answer = {
            "name": "x",
            "entities": [
                {"name": "thing", "count": 2, "fields": [{"name": "blob", "type": "quaternion"}]}
            ],
        }
        assert to_project_data(answer)["entities"]["thing"]["fields"]["blob"]["type"] == "string"

    def test_scale_divides_every_count(self) -> None:
        data = to_project_data(GOOD_ANSWER, scale=1000)
        assert data["entities"]["login_event"]["count"] == 900
        # Never to zero: a scaled-down entity still generates.
        assert data["entities"]["department"]["count"] == 1

    def test_an_absurd_count_is_clamped(self) -> None:
        answer = json.loads(json.dumps(GOOD_ANSWER))
        answer["entities"][2]["count"] = 5_000_000_000
        assert to_project_data(answer)["entities"]["login_event"]["count"] == 50_000_000

    def test_a_proposal_with_no_entities_is_refused(self) -> None:
        with pytest.raises(SchemaProposalError, match="no entities"):
            to_project_data({"name": "x", "entities": []})

    def test_an_entity_with_no_fields_is_refused(self) -> None:
        with pytest.raises(SchemaProposalError, match="no fields"):
            to_project_data({"name": "x", "entities": [{"name": "thing", "count": 1}]})


class TestYaml:
    def test_what_it_writes_it_can_read_back(self) -> None:
        data = to_project_data(GOOD_ANSWER)
        reloaded = load_project_data(_parse_yaml(to_yaml(data)))
        assert list(reloaded.entities) == ["department", "employee", "login_event"]

    @pytest.mark.parametrize(
        "value",
        [
            "yes",
            "no",
            "true",
            "null",
            "on",
            # YAML 1.1 reads this as the integer 750.
            "12:30",
            "1.20",
            "0x1f",
            "a # b",
            "trailing:",
            "- leading dash",
            "{braces}",
            "*anchor",
            "a: b",
        ],
    )
    def test_values_yaml_would_misread_are_quoted(self, value: str) -> None:
        data = to_project_data(
            {
                "name": "x",
                "entities": [
                    {
                        "name": "thing",
                        "count": 1,
                        "fields": [{"name": "note", "type": "string", "semantic": value}],
                    }
                ],
            }
        )
        parsed = _parse_yaml(to_yaml(data))
        assert parsed["entities"]["thing"]["fields"]["note"]["semantic"] == value

    def test_ordinary_prose_is_left_unquoted(self) -> None:
        """Quoting everything would be safe and would read like a machine wrote it."""
        data = to_project_data(
            {
                "name": "Corporate Security",
                "entities": [
                    {
                        "name": "thing",
                        "count": 1,
                        "fields": [
                            {
                                "name": "note",
                                "type": "string",
                                "semantic": "The department this person works in",
                            }
                        ],
                    }
                ],
            }
        )
        text = to_yaml(data)
        assert "semantic: The department this person works in" in text
        assert "name: Corporate Security" in text

    def test_whitespace_and_empty_values_are_dropped_not_preserved(self) -> None:
        """A semantic of three spaces says nothing; it should not reach the file."""
        data = to_project_data(
            {
                "name": "x",
                "entities": [
                    {
                        "name": "thing",
                        "count": 1,
                        "fields": [{"name": "note", "type": "string", "semantic": "   "}],
                    }
                ],
            }
        )
        assert "semantic" not in data["entities"]["thing"]["fields"]["note"]


class TestProposing:
    def test_a_good_answer_becomes_a_compiled_schema(self) -> None:
        proposal = asyncio.run(assistant(json.dumps(GOOD_ANSWER)).propose("a company"))
        assert proposal.ok
        assert proposal.entity_names == ["department", "employee", "login_event"]
        assert proposal.attempts == 1
        assert proposal.lint is not None

    def test_the_proposal_actually_generates(self) -> None:
        """Compiling is necessary; producing records is the real test."""
        proposal = asyncio.run(assistant(json.dumps(GOOD_ANSWER)).propose("a company"))
        assert proposal.compiled is not None
        engine = GenerationEngine(proposal.compiled)
        records = asyncio.run(engine.generate_batch("login_event", 5))
        keys = {
            record.values["employee_id"]
            for record in asyncio.run(engine.generate_batch("employee", 5000))
        }
        assert all(record.values["employee"] in keys for record in records)

    def test_a_bad_answer_is_repaired_on_the_second_attempt(self) -> None:
        proposal = asyncio.run(
            assistant("not json at all {", json.dumps(GOOD_ANSWER)).propose("a company")
        )
        assert proposal.ok
        assert proposal.attempts == 2

    def test_it_gives_up_rather_than_returning_something_broken(self) -> None:
        with pytest.raises(SchemaProposalError, match="attempts"):
            asyncio.run(assistant("nope {", "still nope {").propose("a company"))

    def test_an_empty_description_is_refused_before_any_call(self) -> None:
        with pytest.raises(SchemaProposalError, match="Describe"):
            asyncio.run(assistant(json.dumps(GOOD_ANSWER)).propose("   "))

    def test_notes_report_what_was_changed(self) -> None:
        answer = {
            "name": "x",
            "entities": [
                {
                    "name": "thing",
                    "count": 3,
                    "fields": [{"name": "label", "type": "string", "semantic": "a label"}],
                }
            ],
        }
        proposal = asyncio.run(assistant(json.dumps(answer)).propose("things"))
        assert any("primary key" in note for note in proposal.notes)


class TestProposalSchema:
    def test_the_answer_is_constrained(self) -> None:
        schema = proposal_json_schema()
        assert schema["required"] == ["name", "entities"]
        assert schema["properties"]["entities"]["items"]["required"] == ["name", "count", "fields"]

    def test_every_offered_type_is_one_that_compiles(self) -> None:
        types = schema_types()
        known = {member.value for member in DataType}
        assert set(types) <= known

    def test_each_offered_type_survives_a_round_trip(self) -> None:
        for name in schema_types():
            data = to_project_data(
                {
                    "name": "x",
                    "entities": [
                        {
                            "name": "thing",
                            "count": 2,
                            "fields": [
                                {"name": "key", "type": "integer", "primary_key": True},
                                {
                                    "name": "value",
                                    "type": name,
                                    "choices": ["a", "b"],
                                    "semantic": "a value",
                                },
                            ],
                        }
                    ],
                }
            )
            compiled = compile_project(load_project_data(data))
            assert asyncio.run(GenerationEngine(compiled).generate_batch("thing", 2))


def schema_types() -> list[str]:
    return list(
        proposal_json_schema()["properties"]["entities"]["items"]["properties"]["fields"]["items"][
            "properties"
        ]["type"]["enum"]
    )


def _parse_yaml(text: str) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(text)  # type: ignore[no-any-return]


class TestRunnability:
    """A proposal has to run, not merely compile (design document section 50)."""

    PROSE = json.dumps(
        {
            "name": "Laptop Fleet",
            "entities": [
                {
                    "name": "laptop",
                    "count": 10,
                    "fields": [
                        {"name": "laptop_id", "type": "integer", "primary_key": True},
                        # Reads like prose, so the recommendation engine routes it
                        # to a language model however the type is declared.
                        {
                            "name": "incident_summary",
                            "type": "string",
                            "semantic": (
                                "a paragraph describing what went wrong and how it was investigated"
                            ),
                        },
                    ],
                }
            ],
        }
    )

    def test_a_field_routed_to_a_model_gets_a_provider(self) -> None:
        proposal = asyncio.run(assistant(self.PROSE).propose("laptops"))

        assert "providers" in proposal.data
        provider = next(iter(proposal.data["providers"].values()))
        assert provider["type"] == "language_model"
        assert "providers:" in proposal.yaml

    def test_the_provider_reaches_the_compiled_project(self) -> None:
        proposal = asyncio.run(assistant(self.PROSE).propose("laptops"))
        assert proposal.compiled is not None
        assert proposal.compiled.spec.providers

    def test_it_says_so_rather_than_doing_it_silently(self) -> None:
        proposal = asyncio.run(assistant(self.PROSE).propose("laptops"))
        assert any("language model" in note for note in proposal.notes)

    def test_a_deterministic_proposal_declares_no_provider(self) -> None:
        """No provider block appears where nothing needs one."""
        proposal = asyncio.run(assistant(json.dumps(GOOD_ANSWER)).propose("a company"))
        assert "providers" not in proposal.data
        assert "providers:" not in proposal.yaml

    def test_a_backend_the_assistant_cannot_supply_becomes_a_placeholder(self) -> None:
        """An image field would otherwise abort on the first record."""
        answer = json.dumps(
            {
                "name": "Staff",
                "entities": [
                    {
                        "name": "employee",
                        "count": 5,
                        "fields": [
                            {"name": "employee_id", "type": "integer", "primary_key": True},
                            {"name": "portrait", "type": "string", "semantic": "a headshot"},
                        ],
                    }
                ],
            }
        )
        proposal = asyncio.run(assistant(answer).propose("staff"))
        assert proposal.compiled is not None

        # Whatever the recommendation engine chose, the proposal must produce
        # records without a backend that is not configured.
        engine = GenerationEngine(proposal.compiled, runtime=None)
        records = asyncio.run(engine.generate_batch("employee", 3))
        assert len(records) == 3
