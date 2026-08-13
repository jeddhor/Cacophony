"""End-to-end language-model generation (design document sections 11, 65, 66, 76).

Section 88 asks for integration tests over
``schema -> generator -> validation -> export`` using mock providers. These
drive the whole path with the in-process mock model, so what is asserted is
Cacophony's behaviour - how many calls it makes, how it recovers, what it
caches - rather than whether a real model co-operated.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cacophony.core.errors import GenerationError
from cacophony.core.provenance import ProvenanceMode
from cacophony.generation.engine import GenerationEngine
from cacophony.generation.runtime import GenerationRuntime
from cacophony.providers.cache import CacheMode, GenerationCache
from cacophony.providers.llm.mock import MockLanguageModelProvider
from cacophony.providers.registry import ProviderRegistry
from cacophony.schema.compiler import compile_project
from helpers import make_project

BASE = {
    "ticket": {
        "count": 12,
        "primary_key": "ticket_id",
        "fields": {
            "ticket_id": {"type": "string", "generator": "sequence", "format": "TKT-{00000}"},
            "category": {
                "type": "enum",
                "generator": "weighted",
                "choices": ["VPN", "Email", "Hardware"],
            },
            "severity": {"type": "enum", "generator": "weighted", "choices": ["low", "high"]},
            "summary": {
                "type": "string",
                "semantic": "A one-line subject written by the employee.",
                "generator": "llm",
                "provider": "m",
                "context": ["category", "severity"],
                "constraints": {"min_length": 10, "max_length": 90},
            },
            "resolution_notes": {
                "type": "text",
                "semantic": "Technician notes explaining the resolution.",
                "generator": "llm",
                "provider": "m",
                "context": ["category"],
                "constraints": {"max_length": 200},
            },
        },
    }
}

PROVIDERS = {"m": {"type": "language_model", "adapter": "mock", "model": "mock-1"}}


def build(
    *,
    mode: str = "per_record",
    provider_config: dict[str, Any] | None = None,
    cache: GenerationCache | None = None,
    llm_batch_size: int = 20,
    entities: dict[str, Any] | None = None,
    **engine_kwargs: Any,
) -> tuple[GenerationEngine, MockLanguageModelProvider, GenerationRuntime]:
    """A compiled project wired to an in-process mock model."""
    project = make_project(
        json.loads(json.dumps(entities or BASE)),
        providers=PROVIDERS,
        name="LLM test",
        seed=4242,
    )
    for field_spec in project.entities["ticket"].fields.values():
        if field_spec.generator and field_spec.generator.type == "llm":
            field_spec.generator.options["mode"] = mode

    compiled = compile_project(project)
    registry = ProviderRegistry()
    provider = MockLanguageModelProvider("m", {"model": "mock-1", **(provider_config or {})})
    registry.add(provider)
    runtime = GenerationRuntime(
        project=project,
        providers=registry,
        cache=cache or GenerationCache(mode=CacheMode.DISABLED),
        llm_batch_size=llm_batch_size,
    )
    engine = GenerationEngine(compiled, runtime=runtime, **engine_kwargs)
    return engine, provider, runtime


# --------------------------------------------------------------------------- #
# Section 11: generation modes
# --------------------------------------------------------------------------- #


class TestGenerationModes:
    def test_per_field_makes_one_call_per_field_per_record(self) -> None:
        engine, provider, _ = build(mode="per_field")
        engine.preview("ticket", 6)
        assert len(provider.calls) == 12  # 6 records x 2 fields

    def test_per_record_makes_one_call_per_record(self) -> None:
        engine, provider, _ = build(mode="per_record")
        engine.preview("ticket", 6)
        assert len(provider.calls) == 6

    def test_batch_makes_one_call_per_chunk(self) -> None:
        """Section 11: batching is "much faster"."""
        engine, provider, _ = build(mode="batch", llm_batch_size=3)
        engine.preview("ticket", 12)
        assert len(provider.calls) == 4  # 12 records / 3 per call

    def test_batch_size_is_bounded_by_the_write_batch(self) -> None:
        engine, provider, _ = build(mode="batch", llm_batch_size=100)
        engine.preview("ticket", 5)
        assert len(provider.calls) == 1

    def test_expansion_is_per_record(self) -> None:
        engine, provider, _ = build(mode="expansion")
        engine.preview("ticket", 4)
        assert len(provider.calls) == 4

    @pytest.mark.parametrize("mode", ["per_field", "per_record", "batch"])
    def test_every_mode_fills_every_field(self, mode: str) -> None:
        engine, _, _ = build(mode=mode, llm_batch_size=4)
        records = engine.preview("ticket", 8)
        assert len(records) == 8
        for record in records:
            assert record.values["summary"]
            assert record.values["resolution_notes"]

    @pytest.mark.parametrize("mode", ["per_field", "per_record", "batch"])
    def test_every_mode_produces_varied_content(self, mode: str) -> None:
        """Section 59: repetition is the characteristic failure of these models."""
        engine, _, _ = build(mode=mode, llm_batch_size=4)
        summaries = [record.values["summary"] for record in engine.preview("ticket", 8)]
        assert len(set(summaries)) > 1


# --------------------------------------------------------------------------- #
# Contextual expansion (section 11) and coherence (section 14)
# --------------------------------------------------------------------------- #


class TestContextualExpansion:
    def test_deterministic_fields_are_generated_first(self) -> None:
        """The model is asked to enrich a record, not to invent one."""
        engine, provider, _ = build(mode="per_record")
        engine.preview("ticket", 3)
        for call in provider.calls:
            assert "Known values:" in call.prompt
            assert "category" in call.prompt

    def test_declared_context_reaches_the_prompt(self) -> None:
        engine, provider, _ = build(mode="per_record")
        records = engine.preview("ticket", 1)
        category = records[0].values["category"]
        assert f'"{category}"' in provider.calls[0].prompt

    def test_batch_prompts_carry_every_record_s_context(self) -> None:
        engine, provider, _ = build(mode="batch", llm_batch_size=4)
        engine.preview("ticket", 4)
        prompt = provider.calls[0].prompt
        assert "Record 1:" in prompt and "Record 4:" in prompt

    def test_the_schema_travels_with_the_request(self) -> None:
        engine, provider, _ = build(mode="per_record")
        engine.preview("ticket", 1)
        schema = provider.calls[0].json_schema
        assert set(schema["properties"]) == {"summary", "resolution_notes"}


# --------------------------------------------------------------------------- #
# Section 13: structured output enforcement
# --------------------------------------------------------------------------- #


class TestStructuredOutput:
    def test_constraints_are_enforced_on_model_output(self) -> None:
        engine, _, _ = build(mode="per_record")
        for record in engine.preview("ticket", 8):
            assert 10 <= len(record.values["summary"]) <= 90
            assert len(record.values["resolution_notes"]) <= 200

    def test_records_pass_validation(self) -> None:
        engine, _, _ = build(mode="per_record")
        engine.preview("ticket", 8)
        assert engine.validation_stats()["ticket"]["records_rejected"] == 0


# --------------------------------------------------------------------------- #
# Section 66: the retry ladder
# --------------------------------------------------------------------------- #


class TestRetryLadder:
    """Section 66's ladder, driven by an exact script rather than a dice roll."""

    VALID = json.dumps(
        {"summary": "VPN drops every few minutes", "resolution_notes": "Reissued the profile."}
    )

    def test_a_malformed_answer_is_retried_and_recovers(self) -> None:
        engine, _, runtime = build(
            mode="per_record", provider_config={"responses": ["not json at all", self.VALID]}
        )
        record = engine.preview("ticket", 1)[0]
        assert record.values["summary"] == "VPN drops every few minutes"
        assert runtime.stats.parse_failures == 1
        assert runtime.stats.llm_retries == 1
        assert runtime.stats.fallbacks == 0

    def test_the_second_rung_is_a_repair_prompt(self) -> None:
        engine, provider, _ = build(
            mode="per_record", provider_config={"responses": ["broken", self.VALID]}
        )
        engine.preview("ticket", 1)
        assert len(provider.calls) == 2
        repair = provider.calls[1].prompt
        assert "could not be used" in repair
        assert "What was wrong" in repair
        # The model is shown what it actually returned.
        assert "broken" in repair

    def test_the_third_rung_restates_the_schema(self) -> None:
        engine, provider, _ = build(
            mode="per_record",
            provider_config={"responses": ["broken", "still broken", self.VALID]},
        )
        engine.preview("ticket", 1)
        assert len(provider.calls) == 3
        third = provider.calls[2]
        assert "JSON Schema" in third.prompt
        # Accuracy of format is worth more than variety on the last attempt.
        assert third.temperature is not None and third.temperature < 0.5

    def test_the_ladder_is_finite(self) -> None:
        """Section 66: never permit infinite retry loops."""
        engine, provider, _ = build(
            mode="per_record",
            provider_config={"responses": ["broken"]},
            max_attempts=99,
        )
        with pytest.raises(GenerationError):
            engine.preview("ticket", 1)
        assert len(provider.calls) == 3

    def test_a_constraint_violation_is_retried(self) -> None:
        """Not just unparseable output - output that parses but does not fit."""
        too_short = json.dumps({"summary": "no", "resolution_notes": "fine"})
        engine, _, runtime = build(
            mode="per_record", provider_config={"responses": [too_short, self.VALID]}
        )
        record = engine.preview("ticket", 1)[0]
        assert record.values["summary"] == "VPN drops every few minutes"
        assert runtime.stats.llm_retries == 1

    def test_a_short_batch_is_retried(self) -> None:
        one = json.dumps({"records": [json.loads(self.VALID)]})
        three = json.dumps({"records": [json.loads(self.VALID)] * 3})
        engine, _, runtime = build(
            mode="batch", llm_batch_size=3, provider_config={"responses": [one, three]}
        )
        records = engine.preview("ticket", 3)
        assert all(record.values["summary"] for record in records)
        assert runtime.stats.llm_retries == 1

    def test_every_record_survives_an_intermittent_model(self) -> None:
        engine, _, runtime = build(mode="per_record", provider_config={"malformed_rate": 0.4})
        engine.failure_policy = "skip"
        records = engine.preview("ticket", 12)
        assert len(records) == 12
        assert runtime.stats.llm_retries > 0


def _single_llm_field() -> dict[str, Any]:
    """A minimal entity, so a failure test exercises exactly one call chain."""
    return {
        "ticket": {
            "count": 1,
            "fields": {
                "ticket_id": {"type": "string", "generator": "sequence"},
                "summary": {
                    "type": "string",
                    "semantic": "A subject line.",
                    "generator": "llm",
                    "provider": "m",
                },
            },
        }
    }


# --------------------------------------------------------------------------- #
# Section 65: failure policies
# --------------------------------------------------------------------------- #


class TestUnavailableProvider:
    def _unreachable(self, on_unavailable: str) -> dict[str, Any]:
        entities = json.loads(json.dumps(BASE))
        for name in ("summary", "resolution_notes"):
            entities["ticket"]["fields"][name]["on_unavailable"] = on_unavailable
        return entities

    def test_error_policy_aborts(self) -> None:
        engine, _, _ = build(
            provider_config={"failure_rate": 1.0}, entities=self._unreachable("error")
        )
        with pytest.raises(GenerationError, match="on_unavailable"):
            engine.preview("ticket", 2)

    def test_placeholder_policy_marks_the_value(self) -> None:
        engine, _, runtime = build(
            provider_config={"failure_rate": 1.0}, entities=self._unreachable("placeholder")
        )
        records = engine.preview("ticket", 3)
        assert all("PLACEHOLDER" in record.values["summary"] for record in records)
        assert runtime.stats.fallbacks > 0

    def test_null_policy(self) -> None:
        engine, _, _ = build(
            provider_config={"failure_rate": 1.0}, entities=self._unreachable("null")
        )
        assert all(record.values["summary"] is None for record in engine.preview("ticket", 3))

    def test_an_unreachable_provider_is_asked_once(self) -> None:
        """A downed server must not cost one connection attempt per record."""
        engine, provider, runtime = build(
            provider_config={"failure_rate": 1.0}, entities=self._unreachable("placeholder")
        )
        engine.preview("ticket", 50)
        assert len(provider.calls) == 1
        assert "m" in runtime.unavailable

    def test_a_project_with_no_provider_degrades_cleanly(self) -> None:
        project = make_project(
            {
                "e": {
                    "count": 3,
                    "fields": {
                        "note": {
                            "type": "text",
                            "semantic": "a note",
                            "generator": "llm",
                            "on_unavailable": "placeholder",
                        }
                    },
                }
            }
        )
        engine = GenerationEngine(compile_project(project))
        assert all("PLACEHOLDER" in r.values["note"] for r in engine.preview("e", 3))


# --------------------------------------------------------------------------- #
# Section 76: the cache
# --------------------------------------------------------------------------- #


class TestCacheIntegration:
    def test_identical_requests_hit_the_cache(self) -> None:
        cache = GenerationCache(mode=CacheMode.READ_WRITE)
        engine, provider, _ = build(mode="per_record", cache=cache)
        engine.preview("ticket", 5)
        first_calls = len(provider.calls)
        assert first_calls == 5

        # Same project, same seed, same prompts - so a second pass is free.
        engine2, provider2, runtime2 = build(mode="per_record", cache=cache)
        engine2.preview("ticket", 5)
        assert len(provider2.calls) == 0
        assert runtime2.cache.stats.hits == 5

    def test_cached_values_match_the_originals(self) -> None:
        cache = GenerationCache(mode=CacheMode.READ_WRITE)
        engine, _, _ = build(mode="per_record", cache=cache)
        first = [record.values for record in engine.preview("ticket", 4)]
        engine2, _, _ = build(mode="per_record", cache=cache)
        assert [record.values for record in engine2.preview("ticket", 4)] == first

    def test_disabled_cache_always_calls(self) -> None:
        cache = GenerationCache(mode=CacheMode.DISABLED)
        engine, _, _ = build(mode="per_record", cache=cache)
        engine.preview("ticket", 3)
        engine2, provider2, _ = build(mode="per_record", cache=cache)
        engine2.preview("ticket", 3)
        assert len(provider2.calls) == 3

    def test_a_different_seed_misses(self) -> None:
        cache = GenerationCache(mode=CacheMode.READ_WRITE)
        engine, _, _ = build(mode="per_record", cache=cache)
        engine.preview("ticket", 3)
        engine2, provider2, _ = build(mode="per_record", cache=cache)
        engine2.compiled.spec.project.seed = 999
        engine2.preview("ticket", 3)
        assert len(provider2.calls) == 3

    def test_cache_survives_a_restart(self, tmp_path) -> None:
        path = tmp_path / "cache.db"
        engine, _, _ = build(
            mode="per_record", cache=GenerationCache(path, mode=CacheMode.READ_WRITE)
        )
        engine.preview("ticket", 3)
        engine.runtime.cache.close()

        engine2, provider2, _ = build(
            mode="per_record", cache=GenerationCache(path, mode=CacheMode.READ_WRITE)
        )
        engine2.preview("ticket", 3)
        assert len(provider2.calls) == 0
        engine2.runtime.cache.close()


# --------------------------------------------------------------------------- #
# Determinism and provenance
# --------------------------------------------------------------------------- #


class TestReproducibility:
    def test_same_seed_same_output(self) -> None:
        left, _, _ = build(mode="per_record")
        right, _, _ = build(mode="per_record")
        assert [r.values for r in left.preview("ticket", 6)] == [
            r.values for r in right.preview("ticket", 6)
        ]

    def test_requests_carry_a_derived_seed(self) -> None:
        """Section 4: seeds are preserved so a run can be reproduced."""
        engine, provider, _ = build(mode="per_record")
        engine.preview("ticket", 3)
        seeds = [call.seed for call in provider.calls]
        assert all(seed is not None for seed in seeds)
        assert len(set(seeds)) == 3

    def test_offset_matches_a_full_stream(self) -> None:
        streamed, _, _ = build(mode="per_record")
        resumed, _, _ = build(mode="per_record")
        assert [r.values for r in streamed.preview("ticket", 10)][6:] == [
            r.values for r in resumed.preview("ticket", 4, offset=6)
        ]

    def test_provenance_records_the_provider_and_model(self) -> None:
        engine, _, _ = build(mode="per_record", provenance=ProvenanceMode.FIELD)
        record = engine.preview("ticket", 1)[0]
        provenance = record.provenance.fields["summary"]
        assert provenance.generator == "llm"
        assert provenance.provider == "m"
        assert provenance.model == "mock-1"
        assert provenance.prompt_version >= 1

    def test_full_provenance_keeps_the_prompt_and_response(self) -> None:
        """Section 87: debug material is retained only when asked for."""
        engine, _, _ = build(mode="per_record", provenance=ProvenanceMode.FULL)
        record = engine.preview("ticket", 1)[0]
        block = record.to_dict(provenance_mode=ProvenanceMode.FULL)["_provenance"]
        assert "prompt" in block["fields"]["summary"]
        assert "raw_response" in block["fields"]["summary"]

    def test_field_provenance_withholds_payloads(self) -> None:
        engine, _, _ = build(mode="per_record", provenance=ProvenanceMode.FIELD)
        record = engine.preview("ticket", 1)[0]
        block = record.to_dict(provenance_mode=ProvenanceMode.FIELD)["_provenance"]
        assert "prompt" not in block["fields"]["summary"]


# --------------------------------------------------------------------------- #
# Statistics (sections 58, 86)
# --------------------------------------------------------------------------- #


def test_runtime_statistics_are_reported() -> None:
    engine, _, runtime = build(mode="per_record")
    engine.preview("ticket", 5)
    stats = runtime.stats.to_dict()
    assert stats["llm_calls"] == 5
    assert stats["records_enriched"] == 5
    assert stats["prompt_tokens"] > 0
    assert stats["parse_success_rate"] == 1.0
    assert "providers" in engine.summary()
