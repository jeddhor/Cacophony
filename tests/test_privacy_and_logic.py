"""Sections 57 and 61: what a record must be true of, and what must not look real.

Two categories the validation system was missing, and one detector set the
schema accepted a key for and never used.

Section 57 names six categories; four were built. Logical - "a rule about a
record rather than a value" - is here. Semantic is here too, opt-in and off by
default, and reported as an opinion with the judging model's name attached,
because that is what it is.

Section 61 asked for optional detectors for data that looks real. The schema had
carried a `privacy:` key on a field since the first phase, and nothing read it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cacophony.generation.engine import GenerationEngine
from cacophony.schema.compiler import compile_project
from cacophony.schema.loader import load_project_data
from cacophony.validation.privacy import findings, looks_like_a_real_card


def build(spec: dict[str, Any], **engine_options: Any) -> GenerationEngine:
    compiled = compile_project(load_project_data(spec))
    engine_options.setdefault("validation_policy", "report")
    return GenerationEngine(compiled, **engine_options)


def constant(value: Any, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "generator": "constant", "value": value, **extra}


# --------------------------------------------------------------------------- #
# Logical (section 57)
# --------------------------------------------------------------------------- #

DATES = {
    "project": {"name": "logic", "seed": 1},
    "entities": {
        "job": {
            "count": 3,
            "assertions": [
                {"expr": "ended_on == null or ended_on >= started_on", "message": "ended first"}
            ],
            "fields": {
                "started_on": {"type": "date", "generator": "constant", "value": "2026-05-01"},
                "ended_on": {"type": "date", "generator": "constant", "value": "2026-01-01"},
            },
        }
    },
}


class TestLogicalAssertions:
    def test_a_broken_rule_is_a_validation_failure(self) -> None:
        """Section 57's own example: termination before hire."""
        engine = build(DATES)
        engine.preview("job", 3)
        stats = engine.stats["job"]
        assert stats.rejected == 3
        assert any("ended first" in error for error in stats.errors)

    def test_a_satisfied_rule_says_nothing(self) -> None:
        import copy

        schema = copy.deepcopy(DATES)
        schema["entities"]["job"]["fields"]["ended_on"]["value"] = "2026-09-01"
        engine = build(schema)
        engine.preview("job", 3)
        assert engine.stats["job"].rejected == 0

    def test_null_is_a_literal_because_the_file_is_yaml(self) -> None:
        """`null`, not `None`. A schema has no other Python in it."""
        import copy

        schema = copy.deepcopy(DATES)
        schema["entities"]["job"]["fields"]["ended_on"] = {
            "type": "date",
            "generator": "null",
            "nullable": True,
        }
        engine = build(schema)
        engine.preview("job", 3)
        assert engine.stats["job"].rejected == 0

    def test_it_obeys_the_failure_policy(self) -> None:
        """Which means the default stops the run, like any other invalid record."""
        from cacophony.core.errors import ValidationFailedError

        engine = build(DATES, validation_policy="abort")
        with pytest.raises(ValidationFailedError, match="ended first"):
            engine.preview("job", 3)

    def test_a_rule_that_cannot_be_evaluated_says_so(self) -> None:
        import copy

        schema = copy.deepcopy(DATES)
        schema["entities"]["job"]["assertions"] = [{"expr": "no_such_field > 1"}]
        engine = build(schema)
        engine.preview("job", 1)
        assert any("could not be evaluated" in error for error in engine.stats["job"].errors)

    def test_an_empty_assertion_is_refused_when_the_schema_is_read(self) -> None:
        with pytest.raises(Exception, match="expr"):
            load_project_data(
                {
                    "project": {"name": "x"},
                    "entities": {
                        "e": {
                            "count": 1,
                            "assertions": [{"message": "nothing to check"}],
                            "fields": {"a": {"type": "integer", "generator": "sequence"}},
                        }
                    },
                }
            )

    def test_damage_silences_the_rule_it_broke(self) -> None:
        """Chaos breaks records on purpose; reporting that is reporting a feature."""
        import copy

        schema = copy.deepcopy(DATES)
        schema["chaos"] = {"missing_data": 1.0}
        schema["entities"]["job"]["fields"]["ended_on"]["value"] = "2026-09-01"
        engine = build(schema)
        engine.preview("job", 5)
        assert not any("assertion" in error for error in engine.stats["job"].errors)


# --------------------------------------------------------------------------- #
# Privacy (section 61)
# --------------------------------------------------------------------------- #


class TestTheDetectors:
    @pytest.mark.parametrize(
        "digits,expected",
        [
            ("4111 1111 1111 1111", True),  # the canonical test card, and Luhn-valid
            ("4111 1111 1111 1112", False),
            ("1234 5678 9012 3456", False),
        ],
    )
    def test_luhn(self, digits: str, expected: bool) -> None:
        assert looks_like_a_real_card(digits) is expected

    @pytest.mark.parametrize(
        "value,check",
        [
            ("write to jane@realcompany.com", "domains"),
            ("ssn 123-45-6789", "government_ids"),
            ("card 4111 1111 1111 1111", "card_numbers"),
        ],
    )
    def test_what_looks_real_in_prose_is_found(self, value: str, check: str) -> None:
        """A domain, an identifier or a card number in a sentence is a finding.

        None of the three has an innocent double: a Luhn-valid sixteen-digit run
        is a card number whatever surrounds it.
        """
        assert [kind for kind, _ in findings(value, frozenset({check}))] == [check]

    @pytest.mark.parametrize(
        "value,check,field_type",
        [
            ("8.8.8.8", "addresses", None),
            ("resolved to 8.8.8.8", "addresses", "ip_address"),
            ("(415) 555-2671", "phone_numbers", None),
            ("call (415) 555-2671 today", "phone_numbers", "phone"),
        ],
    )
    def test_addresses_and_numbers_need_the_whole_value_or_the_right_field(
        self, value: str, check: str, field_type: str | None
    ) -> None:
        found = findings(value, frozenset({check}), field_type=field_type)
        assert [kind for kind, _ in found] == [check]

    def test_a_version_string_is_not_an_address(self) -> None:
        """The false positive that would have made this detector unusable.

        `rv:1.9.6.20` in a browser user agent is four octets in range, and a
        detector that reports every version string is a detector people switch
        off - at which point it catches nothing at all.
        """
        agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_2; rv:1.9.6.20) Firefox/3.8"
        assert findings(agent, frozenset({"addresses"})) == []

    @pytest.mark.parametrize(
        "value",
        [
            "write to jane@example.com",
            "and to someone@thing.invalid",
            "ssn 900-45-6789",
            "resolved to 192.0.2.9",
            "or 10.0.0.4",
            "call (415) 555-0142",
        ],
    )
    def test_the_reserved_ranges_are_not_findings(self, value: str) -> None:
        """Everything the generators produce by default has to pass silently."""
        assert (
            findings(value, frozenset(("domains", "government_ids", "addresses", "phone_numbers")))
            == []
        )

    def test_what_the_generators_produce_is_clean(self) -> None:
        """The end-to-end version of the above, over a real template."""
        from pathlib import Path

        from cacophony.schema.loader import load_project

        root = Path(__file__).resolve().parents[1]
        compiled = compile_project(load_project(root / "templates" / "corporate-directory.yaml"))
        engine = GenerationEngine(compiled, validation_policy="report")
        from cacophony.validation.privacy import CHECKS

        every = frozenset(CHECKS)
        for record in engine.preview(compiled.entity_order[0], 25):
            for value in record.values.values():
                assert findings(value, every) == [], value


class TestPrivacyInARun:
    LEAKY = {
        "project": {"name": "leaky", "seed": 2},
        "privacy": {"policy": "warn"},
        "entities": {
            "contact": {
                "count": 4,
                "fields": {
                    "email": constant("someone@realcompany.com"),
                    "safe_email": constant("someone@example.com"),
                },
            }
        },
    }

    def test_findings_are_counted(self) -> None:
        engine = build(self.LEAKY)
        engine.preview("contact", 4)
        report = engine.validation_stats()["contact"]["privacy"]
        assert report["findings"] == {"domains": 4}

    def test_absent_means_not_asked_for(self) -> None:
        import copy

        schema = copy.deepcopy(self.LEAKY)
        del schema["privacy"]
        engine = build(schema)
        engine.preview("contact", 4)
        assert "privacy" not in engine.validation_stats()["contact"]

    def test_block_makes_it_a_failure(self) -> None:
        import copy

        from cacophony.core.errors import ValidationFailedError

        schema = copy.deepcopy(self.LEAKY)
        schema["privacy"]["policy"] = "block"
        engine = build(schema, validation_policy="abort")
        with pytest.raises(ValidationFailedError, match="domain outside the reserved"):
            engine.preview("contact", 4)

    def test_warn_does_not_fail_the_run(self) -> None:
        """A warning is a warning: the record is still written."""
        engine = build(self.LEAKY, validation_policy="abort")
        assert len(engine.preview("contact", 4)) == 4

    def test_a_field_can_say_it_means_to_be_realistic(self) -> None:
        """`safe: false` already made this choice; saying it twice is a trap."""
        import copy

        schema = copy.deepcopy(self.LEAKY)
        schema["entities"]["contact"]["fields"]["email"]["privacy"] = "allow_real"
        engine = build(schema)
        engine.preview("contact", 4)
        assert engine.validation_stats()["contact"]["privacy"]["findings"] == {}

    def test_only_the_asked_for_checks_run(self) -> None:
        import copy

        schema = copy.deepcopy(self.LEAKY)
        schema["privacy"]["checks"] = ["card_numbers"]
        engine = build(schema)
        engine.preview("contact", 4)
        assert engine.validation_stats()["contact"]["privacy"]["findings"] == {}


# --------------------------------------------------------------------------- #
# Semantic (section 57, optional)
# --------------------------------------------------------------------------- #

JUDGED = {
    "project": {"name": "judged", "seed": 3},
    "providers": {
        "writer": {"type": "language_model", "adapter": "mock", "model": "mock-writer"},
        "judge": {
            "type": "language_model",
            "adapter": "mock",
            "model": "mock-judge",
            "options": {"responses": ['{"plausible": true, "reason": "fits"}']},
        },
    },
    "quality": {
        "semantic": {"enabled": True, "sample": 2, "every": 1, "provider": "judge"},
    },
    "entities": {
        "note": {
            "count": 4,
            "fields": {
                "body": {
                    "type": "text",
                    "semantic": "a short note",
                    "generator": "llm",
                    "provider": "writer",
                    "constraints": {"max_length": 120},
                }
            },
        }
    },
}


def judged_engine(spec: dict[str, Any]) -> GenerationEngine:
    from cacophony.generation.runtime import GenerationRuntime

    project = load_project_data(spec)
    return GenerationEngine(
        compile_project(project),
        runtime=GenerationRuntime.for_project(project),
        validation_policy="report",
    )


class TestSemanticIsOptional:
    def test_it_is_off_unless_asked(self) -> None:
        import copy

        schema = copy.deepcopy(JUDGED)
        schema["quality"]["semantic"]["enabled"] = False
        engine = judged_engine(schema)
        engine.preview("note", 4)
        assert asyncio.run(engine.semantic_reports()) == {}

    def test_it_samples_rather_than_judging_everything(self) -> None:
        """Section 57 is careful about cost, so this is bounded and stated."""
        engine = judged_engine(JUDGED)
        engine.preview("note", 4)
        report = asyncio.run(engine.semantic_reports())["note"]
        assert report["sampled_records"] == 2
        assert report["of_records"] == 4

    def test_the_verdict_names_the_model_that_gave_it(self) -> None:
        """An opinion with no attribution is worse than no opinion."""
        engine = judged_engine(JUDGED)
        engine.preview("note", 4)
        report = asyncio.run(engine.semantic_reports())["note"]
        assert report["judged_by"] == ["mock-judge"]
        assert report["rate"] == 1.0

    def test_a_doubted_value_is_quoted_back(self) -> None:
        import copy

        schema = copy.deepcopy(JUDGED)
        schema["providers"]["judge"]["options"]["responses"] = [
            '{"plausible": false, "reason": "no connection to the record"}'
        ]
        engine = judged_engine(schema)
        engine.preview("note", 4)
        report = asyncio.run(engine.semantic_reports())["note"]
        assert report["rate"] == 0.0
        assert report["examples"][0]["reason"] == "no connection to the record"

    def test_a_threshold_is_reported_as_met_or_not(self) -> None:
        import copy

        schema = copy.deepcopy(JUDGED)
        schema["quality"]["semantic"]["threshold"] = 0.9
        engine = judged_engine(schema)
        engine.preview("note", 4)
        assert asyncio.run(engine.semantic_reports())["note"]["meets_threshold"] is True

    def test_an_unreachable_judge_is_reported_not_raised(self) -> None:
        """A run must not fail because an opinion could not be obtained."""
        import copy

        schema = copy.deepcopy(JUDGED)
        schema["providers"]["judge"]["options"] = {"healthy": False, "failure_rate": 1.0}
        engine = judged_engine(schema)
        engine.preview("note", 4)
        report = asyncio.run(engine.semantic_reports())["note"]
        assert report["judged"] == 0 or report.get("error")


def _asset_store():
    import tempfile

    from cacophony.assets.store import AssetStore

    return AssetStore(tempfile.mkdtemp())


class TestEveryTemplateIsClean:
    """The detectors, turned on the eight templates the project ships.

    This is the check that found a real leak the first time it ran:
    `corporate-directory.yaml` used Faker's `phone_number`, whose output is
    well-formed and dialable, and Faker's safe mode only rewrote domains. A
    template is what people copy, so a template that leaks is a leak with a
    multiplier on it.
    """

    @pytest.mark.parametrize(
        "template",
        [
            "corporate-directory.yaml",
            "helpdesk.yaml",
            "retail-commerce.yaml",
            "security-operations.yaml",
            "saas-application.yaml",
            "iot-telemetry.yaml",
            "multimodal-support.yaml",
            "conversational-ai.yaml",
        ],
    )
    def test_nothing_it_generates_looks_real(self, template: str) -> None:
        from pathlib import Path

        from cacophony.schema.loader import load_project
        from cacophony.validation.privacy import CHECKS

        root = Path(__file__).resolve().parents[1]
        compiled = compile_project(load_project(root / "templates" / template))
        engine = GenerationEngine(
            compiled,
            validation_policy="report",
            # Some templates write media, and a generator with nowhere to put a
            # file refuses rather than guessing.
            assets=_asset_store(),
        )
        every = frozenset(CHECKS)

        for name in compiled.entity_order:
            for record in engine.preview(name, 10):
                types = {f.name: str(f.spec.type) for f in compiled.entity(name).fields}
                for field_name, value in record.values.items():
                    assert findings(value, every, field_type=types.get(field_name)) == [], (
                        f"{template}:{name}.{field_name} = {value!r}"
                    )

    def test_fakers_safe_mode_covers_phones_and_identifiers(self) -> None:
        """Not only domains, which is all it used to cover."""
        schema = {
            "project": {"name": "faker safety", "seed": 5},
            "entities": {
                "person": {
                    "count": 20,
                    "fields": {
                        "phone": {
                            "type": "phone",
                            "generator": "faker",
                            "provider": "phone_number",
                        },
                        "ssn": {"type": "string", "generator": "faker", "provider": "ssn"},
                    },
                }
            },
        }
        engine = build(schema)
        for record in engine.preview("person", 20):
            assert findings(record.values["phone"], frozenset({"phone_numbers"})) == []
            assert findings(record.values["ssn"], frozenset({"government_ids"})) == []

    def test_asking_for_realistic_values_still_works(self) -> None:
        """`safe: false` is a legitimate need, and it must still be obeyed."""
        schema = {
            "project": {"name": "deliberate", "seed": 5},
            "entities": {
                "person": {
                    "count": 20,
                    "fields": {
                        "phone": {
                            "type": "phone",
                            "generator": "faker",
                            "provider": "phone_number",
                            "safe": False,
                        }
                    },
                }
            },
        }
        engine = build(schema)
        values = [record.values["phone"] for record in engine.preview("person", 20)]
        assert any(findings(value, frozenset({"phone_numbers"})) for value in values)
