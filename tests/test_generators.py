"""Built-in generators (design document section 8)."""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import re
from collections import Counter
from typing import Any

import pytest

from cacophony.core.errors import GenerationError, GeneratorConfigError, GeneratorNotFoundError
from cacophony.core.types import DataType
from cacophony.generation.registry import REGISTRY
from cacophony.schema.models import ConstraintSpec, FieldSpec
from helpers import make_context


def build(name: str, options: dict[str, Any] | None = None, **field_keys: Any) -> Any:
    field_spec = FieldSpec(name="value", **field_keys)
    return REGISTRY.create(name, options or {}, field=field_spec), field_spec


def draw(name: str, options: dict[str, Any] | None = None, *, index: int = 0, **field_keys: Any):
    generator, field_spec = build(name, options, **field_keys)
    return generator.generate_sync(make_context(field_spec, record_index=index))


def draw_many(name: str, options: dict[str, Any] | None = None, count: int = 200, **field_keys):
    generator, field_spec = build(name, options, **field_keys)
    return [
        generator.generate_sync(make_context(field_spec, record_index=index))
        for index in range(count)
    ]


class TestRegistry:
    def test_unknown_generator_lists_alternatives(self) -> None:
        with pytest.raises(GeneratorNotFoundError, match="Available generators"):
            REGISTRY.create("does-not-exist")

    def test_aliases_resolve(self) -> None:
        assert REGISTRY.resolve("rand") == "random"
        assert REGISTRY.resolve("fk") == "reference"

    def test_every_generator_has_a_summary(self) -> None:
        for row in REGISTRY.describe():
            assert row["summary"], f"{row['name']} has no docstring"


class TestConstant:
    def test_returns_the_value(self) -> None:
        assert draw("constant", {"value": "fixed"}) == "fixed"

    def test_is_stable_across_records(self) -> None:
        assert set(draw_many("constant", {"value": 7}, 20)) == {7}


class TestSequence:
    def test_plain_integers(self) -> None:
        assert draw_many("sequence", {}, 4, type=DataType.INTEGER) == [1, 2, 3, 4]

    def test_start_and_step(self) -> None:
        assert draw_many("sequence", {"start": 10, "step": 5}, 3, type=DataType.INTEGER) == [
            10,
            15,
            20,
        ]

    def test_format_from_section_3(self) -> None:
        assert draw("sequence", {"format": "EMP-{000000}"}, index=0) == "EMP-000001"
        assert draw("sequence", {"format": "EMP-{000000}"}, index=48290) == "EMP-048291"

    def test_format_from_section_8(self) -> None:
        assert draw("sequence", {"format": "USER-{000000}"}, index=1) == "USER-000002"

    def test_index_addressable_not_stateful(self) -> None:
        """Record 4,823,913 has the same id however it was reached."""
        generator, field_spec = build("sequence", {"format": "E-{00000000}"})
        direct = generator.generate_sync(make_context(field_spec, record_index=4_823_913))
        assert direct == "E-04823914"

    def test_format_without_a_token_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="padding token"):
            build("sequence", {"format": "EMP-"})

    def test_zero_step_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="step"):
            build("sequence", {"step": 0})


class TestUuid:
    def test_is_deterministic_for_a_seed(self) -> None:
        first = draw("uuid", type=DataType.UUID)
        second = draw("uuid", type=DataType.UUID)
        assert first == second

    def test_differs_between_records(self) -> None:
        values = draw_many("uuid", {}, 50, type=DataType.UUID)
        assert len(set(values)) == 50

    def test_version_5_is_stable_for_a_name(self) -> None:
        generator, field_spec = build("uuid", {"version": 5, "name": "key"})
        context = make_context(field_spec, values={"key": "alice"})
        assert generator.generate_sync(context) == generator.generate_sync(context)

    def test_bad_version_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError):
            build("uuid", {"version": 9})


class TestRandom:
    def test_integer_respects_bounds(self) -> None:
        values = draw_many("random", {"min": 5, "max": 9}, 300, type=DataType.INTEGER)
        assert all(5 <= value <= 9 for value in values)
        assert len(set(values)) > 1

    def test_float_precision(self) -> None:
        values = draw_many("random", {"min": 0, "max": 1, "precision": 2}, 50, type=DataType.FLOAT)
        assert all(round(value, 2) == value for value in values)

    def test_string_length_bounds(self) -> None:
        values = draw_many("random", {"min_length": 4, "max_length": 8}, 100, type=DataType.STRING)
        assert all(4 <= len(value) <= 8 for value in values)

    def test_charset(self) -> None:
        values = draw_many("random", {"charset": "hex", "length": 12}, 30, type=DataType.STRING)
        assert all(re.fullmatch(r"[0-9a-f]{12}", value) for value in values)

    def test_inverted_bounds_are_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="greater than"):
            build("random", {"min": 10, "max": 1}, type=DataType.INTEGER)

    def test_reads_field_constraints_when_no_options_given(self) -> None:
        values = draw_many(
            "random", {}, 100, type=DataType.INTEGER, constraints=ConstraintSpec(min=50, max=52)
        )
        assert all(50 <= value <= 52 for value in values)

    def test_temporal_field_delegates_to_the_datetime_generator(self) -> None:
        value = draw("random", {"start": "2026-01-01", "end": "2026-01-31"}, type=DataType.DATE)
        assert isinstance(value, dt.date)


class TestBoolean:
    def test_probability_is_approximately_honoured(self) -> None:
        values = draw_many("boolean", {"probability": 0.25}, 2000, type=DataType.BOOLEAN)
        assert 0.20 < sum(values) / len(values) < 0.30

    def test_probability_out_of_range_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError):
            build("boolean", {"probability": 1.5})


class TestWeighted:
    def test_section_8_operating_system_weights(self) -> None:
        counts = Counter(
            draw_many(
                "weighted",
                {"choices": {"Windows": 67, "macOS": 18, "Linux": 13, "Other": 2}},
                4000,
            )
        )
        share = {key: value / 4000 for key, value in counts.items()}
        assert 0.62 < share["Windows"] < 0.72
        assert 0.14 < share["macOS"] < 0.22
        assert 0.09 < share["Linux"] < 0.17

    def test_plain_list_gives_equal_weights(self) -> None:
        counts = Counter(draw_many("weighted", {"choices": ["a", "b"]}, 2000))
        assert 0.44 < counts["a"] / 2000 < 0.56

    def test_list_of_mappings(self) -> None:
        values = set(draw_many("weighted", {"choices": [{"value": "x", "weight": 3}]}, 10))
        assert values == {"x"}

    def test_zero_weight_is_never_chosen(self) -> None:
        values = set(draw_many("weighted", {"choices": {"yes": 10, "never": 0}}, 1500))
        assert values == {"yes"}

    def test_falls_back_to_the_field_enum(self) -> None:
        values = set(
            draw_many("weighted", {}, 100, constraints=ConstraintSpec(enum=["a", "b", "c"]))
        )
        assert values <= {"a", "b", "c"}

    def test_empty_choices_are_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError):
            build("weighted", {"choices": []})

    def test_distribution_reports_normalised_weights(self) -> None:
        generator, _ = build("weighted", {"choices": {"a": 3, "b": 1}})
        assert generator.distribution() == pytest.approx({"a": 0.75, "b": 0.25})


class TestDistribution:
    def test_normal_is_centred(self) -> None:
        values = draw_many(
            "distribution",
            {"distribution": "normal", "mean": 100, "stddev": 10},
            3000,
            type=DataType.FLOAT,
        )
        assert 97 < sum(values) / len(values) < 103

    def test_clamping(self) -> None:
        values = draw_many(
            "distribution",
            {"distribution": "normal", "mean": 40, "stddev": 30, "min": 18, "max": 65},
            1000,
            type=DataType.INTEGER,
        )
        assert all(18 <= value <= 65 for value in values)

    def test_integer_fields_get_integers(self) -> None:
        values = draw_many(
            "distribution", {"distribution": "normal", "mean": 5}, 20, type=DataType.INTEGER
        )
        assert all(isinstance(value, int) for value in values)

    @pytest.mark.parametrize(
        "options",
        [
            {"distribution": "uniform", "min": 0, "max": 1},
            {"distribution": "lognormal", "mean": 1, "stddev": 0.5},
            {"distribution": "exponential", "rate": 2},
            {"distribution": "poisson", "lam": 3},
            {"distribution": "beta", "alpha": 2, "beta": 5},
        ],
    )
    def test_every_distribution_produces_numbers(self, options: dict[str, Any]) -> None:
        values = draw_many("distribution", options, 100, type=DataType.FLOAT)
        assert all(isinstance(value, (int, float)) for value in values)

    def test_histogram(self) -> None:
        values = draw_many(
            "distribution",
            {
                "distribution": "histogram",
                "bins": [{"value": 1, "weight": 9}, {"value": 100, "weight": 1}],
            },
            2000,
            type=DataType.INTEGER,
        )
        assert set(values) == {1, 100}
        assert 0.85 < values.count(1) / len(values) < 0.95

    def test_unknown_distribution_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="must be one of"):
            build("distribution", {"distribution": "wishful"})

    def test_histogram_without_bins_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="bins"):
            build("distribution", {"distribution": "histogram"})


class TestPattern:
    def test_section_8_example(self) -> None:
        values = draw_many("pattern", {"pattern": "SRV-{A-Z}{A-Z}-{0000}"}, 50)
        assert all(re.fullmatch(r"SRV-[A-Z]{2}-\d{4}", value) for value in values)

    def test_repeat_suffix(self) -> None:
        assert re.fullmatch(r"[a-z]{5}", draw("pattern", {"pattern": "{a-z:5}"}))

    def test_letter_and_alphanumeric_tokens(self) -> None:
        assert re.fullmatch(r"[A-Z]{3}", draw("pattern", {"pattern": "{???}"}))
        assert re.fullmatch(r"[A-Z0-9]{4}", draw("pattern", {"pattern": "{****}"}))

    def test_escaped_braces(self) -> None:
        assert draw("pattern", {"pattern": "{{literal}}"}) == "{literal}"

    def test_is_deterministic(self) -> None:
        options = {"pattern": "{A-Z:6}"}
        assert draw("pattern", options, index=3) == draw("pattern", options, index=3)

    def test_varies_between_records(self) -> None:
        assert len(set(draw_many("pattern", {"pattern": "{A-Z:8}"}, 100))) > 90

    def test_unknown_token_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="unrecognised pattern token"):
            build("pattern", {"pattern": "{banana}"})

    def test_reversed_range_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="reversed"):
            build("pattern", {"pattern": "{Z-A}"})


class TestTemplate:
    def test_interpolates_and_declares_dependencies(self) -> None:
        generator, field_spec = build(
            "template", {"template": "{first_name|lower}.{last_name|lower}@example.com"}
        )
        assert set(generator.dependencies()) == {"first_name", "last_name"}
        context = make_context(field_spec, values={"first_name": "Ada", "last_name": "Lovelace"})
        assert generator.generate_sync(context) == "ada.lovelace@example.com"

    def test_filters(self) -> None:
        generator, field_spec = build(
            "template", {"template": "{a|upper}-{b|initial}-{c|slug}-{d|pad:4}"}
        )
        context = make_context(field_spec, values={"a": "hi", "b": "Zoe", "c": "New York!", "d": 7})
        assert generator.generate_sync(context) == "HI-Z-new-york-0007"

    def test_missing_value_becomes_empty_by_default(self) -> None:
        generator, field_spec = build("template", {"template": "x{missing}y"})
        assert generator.generate_sync(make_context(field_spec)) == "xy"

    def test_missing_value_can_raise(self) -> None:
        generator, field_spec = build("template", {"template": "{missing}", "on_missing": "error"})
        with pytest.raises(GenerationError, match="null"):
            generator.generate_sync(make_context(field_spec))

    def test_template_with_no_placeholders_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="interpolates nothing"):
            build("template", {"template": "static"})


class TestExpression:
    def test_section_8_example(self) -> None:
        generator, field_spec = build(
            "expression",
            {"expression": 'lower(first_name + "." + last_name + "@" + company_domain)'},
        )
        assert set(generator.dependencies()) == {"first_name", "last_name", "company_domain"}
        context = make_context(
            field_spec,
            values={
                "first_name": "Samantha",
                "last_name": "Ortiz",
                "company_domain": "acme.example",
            },
        )
        assert generator.generate_sync(context) == "samantha.ortiz@acme.example"

    def test_arithmetic_and_conditionals(self) -> None:
        generator, field_spec = build(
            "expression", {"expression": 'iif(age >= 18, "adult", "minor")'}
        )
        assert generator.generate_sync(make_context(field_spec, values={"age": 30})) == "adult"
        assert generator.generate_sync(make_context(field_spec, values={"age": 9})) == "minor"

    def test_unknown_name_names_the_field(self) -> None:
        generator, field_spec = build("expression", {"expression": "mystery"})
        with pytest.raises(GenerationError, match="mystery"):
            generator.generate_sync(make_context(field_spec))

    @pytest.mark.parametrize(
        "source",
        [
            "__import__('os').system('true')",
            "[x for x in range(3)]",
            "open('/etc/passwd')",
            "(lambda: 1)()",
            "first_name.__class__",
            "company._record",
        ],
    )
    def test_dangerous_syntax_is_refused_at_compile_time(self, source: str) -> None:
        """The expression evaluator is an allow-list, not a blocklist."""
        with pytest.raises(GeneratorConfigError):
            build("expression", {"expression": source})

    def test_unknown_function_is_refused(self) -> None:
        with pytest.raises(GeneratorConfigError, match="unknown function"):
            build("expression", {"expression": "eval('1')"})

    def test_syntax_error_is_reported_clearly(self) -> None:
        with pytest.raises(GeneratorConfigError, match="could not parse"):
            build("expression", {"expression": "1 +"})


class TestLookup:
    def test_random_mode_stays_inside_the_table(self) -> None:
        values = set(draw_many("lookup", {"values": ["a", "b", "c"]}, 100))
        assert values <= {"a", "b", "c"}

    def test_cycle_mode_walks_in_order(self) -> None:
        assert draw_many("lookup", {"values": [1, 2, 3], "mode": "cycle"}, 5) == [1, 2, 3, 1, 2]

    def test_csv_source(self, tmp_path) -> None:
        path = tmp_path / "cities.csv"
        path.write_text("city,country\nDenver,US\nAustin,US\n", encoding="utf-8")
        values = set(draw_many("lookup", {"path": str(path), "column": "city"}, 20))
        assert values <= {"Denver", "Austin"}

    def test_json_source(self, tmp_path) -> None:
        path = tmp_path / "items.json"
        path.write_text('["x", "y"]', encoding="utf-8")
        assert set(draw_many("lookup", {"path": str(path)}, 20)) <= {"x", "y"}

    def test_missing_file_is_reported(self, tmp_path) -> None:
        with pytest.raises(GeneratorConfigError, match="not found"):
            build("lookup", {"path": str(tmp_path / "nope.csv")})

    def test_no_source_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="values"):
            build("lookup", {})


class TestTimestamp:
    def test_stays_inside_the_window(self) -> None:
        values = draw_many(
            "datetime",
            {"start": "2026-01-01T00:00:00", "end": "2026-01-31T23:59:59"},
            200,
            type=DataType.DATETIME,
        )
        assert all(dt.datetime(2026, 1, 1) <= value <= dt.datetime(2026, 2, 1) for value in values)

    def test_date_fields_get_dates(self) -> None:
        value = draw("datetime", {"start": "2026-01-01", "end": "2026-02-01"}, type=DataType.DATE)
        assert isinstance(value, dt.date) and not isinstance(value, dt.datetime)

    def test_weekdays_only(self) -> None:
        values = draw_many(
            "datetime",
            {"start": "2026-01-01", "end": "2026-06-30", "weekdays_only": True},
            300,
            type=DataType.DATETIME,
        )
        weekend = sum(1 for value in values if value.weekday() >= 5)
        # Bounded resampling means a handful may slip through rather than loop.
        assert weekend / len(values) < 0.02

    def test_business_hours_weighting(self) -> None:
        values = draw_many(
            "datetime",
            {"start": "2026-01-01", "end": "2026-03-01", "business_hours": True},
            1000,
            type=DataType.DATETIME,
        )
        in_hours = sum(1 for value in values if 7 <= value.hour <= 18)
        assert in_hours / len(values) > 0.7

    def test_reversed_window_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="after end"):
            build("datetime", {"start": "2026-02-01", "end": "2026-01-01"})

    def test_unparseable_bound_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="ISO-8601"):
            build("datetime", {"start": "last tuesday"})


class TestNetworkAndIdentifiers:
    def test_ip_defaults_to_documentation_ranges(self) -> None:
        """Section 62: a synthetic log must not point at a real host."""
        documentation = [
            ipaddress.ip_network(cidr)
            for cidr in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
        ]
        for value in draw_many("ip", {}, 100, type=DataType.IP_ADDRESS):
            address = ipaddress.ip_address(value)
            assert any(address in network for network in documentation)

    def test_ip_honours_an_explicit_network(self) -> None:
        network = ipaddress.ip_network("10.40.0.0/16")
        for value in draw_many("ip", {"network": "10.40.0.0/16"}, 100, type=DataType.IP_ADDRESS):
            assert ipaddress.ip_address(value) in network

    def test_ipv6_documentation_prefix(self) -> None:
        value = draw("ip", {"version": 6}, type=DataType.IP_ADDRESS)
        assert ipaddress.ip_address(value) in ipaddress.ip_network("2001:db8::/32")

    def test_bad_network_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="CIDR"):
            build("ip", {"network": "not-a-network"})

    def test_mac_defaults_to_the_documentation_range(self) -> None:
        for value in draw_many("mac", {}, 50, type=DataType.MAC_ADDRESS):
            assert value.startswith("00:00:5e:00:53:")

    def test_mac_honours_an_oui(self) -> None:
        for value in draw_many("mac", {"oui": "00:1a:2b"}, 20, type=DataType.MAC_ADDRESS):
            assert value.startswith("00:1a:2b:")

    def test_phone_uses_the_fictitious_block(self) -> None:
        """Section 62: generated numbers must not ring a real telephone."""
        for value in draw_many("phone", {"format": "national"}, 100):
            assert re.fullmatch(r"\(\d{3}\) 555-01\d{2}", value)

    def test_government_id_uses_a_never_issued_range(self) -> None:
        for value in draw_many("government_id", {}, 100):
            assert 900 <= int(value.split("-")[0]) <= 999

    def test_government_id_masking(self) -> None:
        assert draw("government_id", {"masked": True}).startswith("***-**-")


class TestFaker:
    def test_is_reproducible_for_a_seed(self) -> None:
        assert draw("faker", {"provider": "first_name"}) == draw(
            "faker", {"provider": "first_name"}
        )

    def test_varies_between_records(self) -> None:
        values = draw_many("faker", {"provider": "name"}, 50)
        assert len(set(values)) > 25

    def test_emails_are_rewritten_onto_reserved_domains(self) -> None:
        """Section 62: no generated address may reach a real mailbox."""
        for value in draw_many("faker", {"provider": "email"}, 100, type=DataType.EMAIL):
            domain = value.split("@")[1]
            assert domain.endswith((".example", ".test", ".invalid")) or domain in {
                "example.com",
                "example.org",
                "example.net",
            }

    def test_safety_can_be_disabled_explicitly(self) -> None:
        values = draw_many("faker", {"provider": "email", "safe": False}, 40)
        assert any(not value.endswith(".example") for value in values)

    def test_unknown_provider_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="no provider named"):
            build("faker", {"provider": "definitely_not_a_provider"})

    def test_missing_provider_option_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="required"):
            build("faker", {})


class TestTransformAndComposite:
    def test_transform_reads_a_source_field(self) -> None:
        generator, field_spec = build(
            "transform", {"source": "name", "operations": ["uppercase", "truncate:3"]}
        )
        assert generator.dependencies() == ("name",)
        context = make_context(field_spec, values={"name": "cacophony"})
        assert generator.generate_sync(context) == "CAC"

    def test_transform_masking(self) -> None:
        generator, field_spec = build("transform", {"source": "n", "operations": ["mask:4"]})
        assert generator.generate_sync(make_context(field_spec, values={"n": "123456789"})) == (
            "*****6789"
        )

    def test_unknown_operation_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="unknown transformation"):
            build("transform", {"operations": ["levitate"]})

    def test_composite_threads_the_value_through(self) -> None:
        import asyncio

        generator, field_spec = build(
            "composite",
            {
                "steps": [
                    {"type": "constant", "value": "  Hello World  "},
                    {"type": "transform", "operations": ["strip", "lowercase"]},
                ]
            },
        )
        produced = asyncio.run(generator.generate(make_context(field_spec)))
        assert produced.value == "hello world"

    def test_composite_without_steps_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="steps"):
            build("composite", {"steps": []})


class TestPendingGenerators:
    """Failure policies for generators that need something a run may lack.

    ``image`` and ``tts`` are implemented (see test_media.py); what is checked
    here is section 65's policy list when the backend is absent. ``script``
    remains unimplemented, waiting on isolation rather than on a backend.
    """

    def test_image_errors_by_default_without_a_store(self) -> None:
        generator, field_spec = build("image", {}, type=DataType.IMAGE)
        with pytest.raises(GenerationError, match="asset store"):
            asyncio.run(generator.generate(make_context(field_spec)))

    def test_placeholder_policy_produces_a_marked_value(self) -> None:
        generator, field_spec = build(
            "image", {"on_unavailable": "placeholder"}, type=DataType.IMAGE
        )
        produced = asyncio.run(generator.generate(make_context(field_spec)))
        assert "placeholder" in produced.value

    def test_placeholder_respects_length_constraints(self) -> None:
        value = draw(
            "reference",
            {"entity": "other", "on_unavailable": "placeholder"},
            constraints=ConstraintSpec(max_length=12, min_length=8),
        )
        assert 8 <= len(value) <= 12

    def test_null_policy(self) -> None:
        generator, field_spec = build("image", {"on_unavailable": "null"}, type=DataType.IMAGE)
        assert asyncio.run(generator.generate(make_context(field_spec))).value is None

    def test_script_is_still_pending(self) -> None:
        generator, field_spec = build("script", {"code": "return 1"})
        with pytest.raises(GenerationError, match="plugin phase"):
            generator.generate_sync(make_context(field_spec))

    def test_reference_requires_a_target_entity(self) -> None:
        with pytest.raises(GeneratorConfigError, match="entity"):
            build("reference", {})

    def test_script_requires_code_or_path(self) -> None:
        with pytest.raises(GeneratorConfigError, match="code"):
            build("script", {})

    def test_tts_declares_its_source_as_a_dependency(self) -> None:
        generator, _ = build("tts", {"source": "greeting_text"}, type=DataType.AUDIO)
        assert "greeting_text" in generator.dependencies()


class TestLanguageModelGenerator:
    """Option handling only; behaviour is covered in test_providers.py."""

    def test_modes(self) -> None:
        for mode in ("per_field", "per_record", "batch"):
            generator, _ = build("llm", {"mode": mode}, type=DataType.TEXT)
            assert generator.mode == mode

    def test_expansion_is_per_record(self) -> None:
        """Section 11's contextual expansion is what every mode already does."""
        generator, _ = build("llm", {"mode": "expansion"}, type=DataType.TEXT)
        assert generator.mode == "per_record"

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(GeneratorConfigError, match="must be one of"):
            build("llm", {"mode": "telepathy"})

    def test_temperature_bounds(self) -> None:
        with pytest.raises(GeneratorConfigError, match="temperature"):
            build("llm", {"temperature": 5})

    def test_max_tokens_must_be_positive(self) -> None:
        with pytest.raises(GeneratorConfigError, match="max_tokens"):
            build("llm", {"max_tokens": 0})

    def test_declares_its_context_as_dependencies(self) -> None:
        generator, _ = build("llm", {"context": ["a", "b"]}, type=DataType.TEXT)
        assert tuple(generator.dependencies()) == ("a", "b")

    def test_without_a_runtime_the_policy_applies(self) -> None:
        import asyncio

        generator, field_spec = build(
            "llm", {"on_unavailable": "placeholder"}, type=DataType.TEXT, semantic="a bio"
        )
        produced = asyncio.run(generator.generate(make_context(field_spec)))
        assert "PLACEHOLDER" in produced.value

    def test_without_a_runtime_the_error_names_the_remedy(self) -> None:
        import asyncio

        generator, field_spec = build("llm", {}, type=DataType.TEXT)
        with pytest.raises(GenerationError, match="providers:"):
            asyncio.run(generator.generate(make_context(field_spec)))
