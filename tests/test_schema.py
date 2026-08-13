"""Schema parsing, compilation, planning and linting (sections 3.1, 100, 102)."""

from __future__ import annotations

import pytest

from cacophony.core.errors import (
    CircularDependencyError,
    GeneratorConfigError,
    GeneratorNotFoundError,
    SchemaError,
    UnknownFieldReferenceError,
)
from cacophony.core.types import DataType
from cacophony.schema.compiler import compile_project
from cacophony.schema.linter import Severity, lint_project
from cacophony.schema.loader import dump_project, load_project, load_project_data, save_project
from cacophony.schema.models import RelationshipSpec
from helpers import compile_from, make_project


class TestParsing:
    def test_section_3_canonical_schema(self) -> None:
        """The exact YAML shape from design document section 3.1."""
        project = load_project_data(
            {
                "project": {"name": "Example Corporate Dataset"},
                "entities": {
                    "employee": {
                        "count": 10000,
                        "fields": {
                            "employee_id": {
                                "type": "string",
                                "generator": "sequence",
                                "format": "EMP-{000000}",
                            },
                            "first_name": {
                                "type": "string",
                                "semantic": "Person's given name",
                            },
                            "biography": {
                                "type": "text",
                                "generator": "llm",
                                "semantic": "A short fictional professional biography",
                            },
                        },
                    }
                },
            }
        )
        employee = project.entities["employee"]
        assert employee.count == 10000
        # `format` sits beside `generator` and must land in the generator options.
        assert employee.fields["employee_id"].generator.options["format"] == "EMP-{000000}"
        assert employee.fields["first_name"].generator is None
        assert employee.fields["biography"].generator.type == "llm"

    def test_explicit_generator_mapping_form(self) -> None:
        project = make_project(
            {
                "e": {
                    "fields": {
                        "f": {"generator": {"type": "sequence", "format": "X-{000}"}},
                    }
                }
            }
        )
        assert project.entities["e"].fields["f"].generator.options["format"] == "X-{000}"

    def test_single_key_generator_form(self) -> None:
        project = make_project({"e": {"fields": {"f": {"generator": {"sequence": {"start": 5}}}}}})
        generator = project.entities["e"].fields["f"].generator
        assert generator.type == "sequence" and generator.options["start"] == 5

    def test_names_are_stamped_from_keys(self) -> None:
        project = make_project({"widget": {"fields": {"colour": {"type": "string"}}}})
        assert project.entities["widget"].name == "widget"
        assert project.entities["widget"].fields["colour"].name == "colour"

    def test_unknown_type_is_reported_readably(self) -> None:
        with pytest.raises(SchemaError, match=r"employee\.fields\.f\.type"):
            make_project({"employee": {"fields": {"f": {"type": "banana"}}}})

    def test_null_probability_bounds(self) -> None:
        with pytest.raises(SchemaError, match="null_probability"):
            make_project({"e": {"fields": {"f": {"null_probability": 2.0}}}})

    def test_inline_credentials_are_refused(self) -> None:
        """Section 63: project files must never carry a credential value."""
        with pytest.raises(SchemaError, match="logical secret id"):
            load_project_data(
                {
                    "project": {"name": "p"},
                    "entities": {"e": {"fields": {"f": {"type": "string"}}}},
                    "providers": {"llm": {"secret": "sk-" + "a" * 40}},
                }
            )


class TestLoading:
    def test_missing_file(self, tmp_path) -> None:
        with pytest.raises(SchemaError, match="not found"):
            load_project(tmp_path / "nope.yaml")

    def test_empty_file(self, tmp_path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        with pytest.raises(SchemaError, match="empty"):
            load_project(path)

    def test_invalid_yaml(self, tmp_path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("project: [unclosed\n", encoding="utf-8")
        with pytest.raises(SchemaError, match="invalid YAML"):
            load_project(path)

    def test_unsupported_suffix(self, tmp_path) -> None:
        path = tmp_path / "project.toml"
        path.write_text("x = 1", encoding="utf-8")
        with pytest.raises(SchemaError, match="Unsupported project file type"):
            load_project(path)

    def test_round_trip(self, tmp_path, corporate_project) -> None:
        """Section 74: projects must survive a Git-friendly round trip."""
        path = save_project(corporate_project, tmp_path / "copy.yaml")
        reloaded = load_project(path)
        assert reloaded.project.name == corporate_project.project.name
        assert set(reloaded.entities) == set(corporate_project.entities)
        assert reloaded.entities["employee"].count == 5000

    def test_dump_omits_defaults(self, corporate_project) -> None:
        """A diff should show what changed, not every default in the model."""
        text = dump_project(corporate_project)
        assert "scenarios" not in text
        assert "Corporate Directory" in text


class TestCompilation:
    def test_orders_fields_by_dependency(self) -> None:
        compiled = compile_from(
            {
                "person": {
                    "count": 5,
                    "fields": {
                        "email": {
                            "type": "email",
                            "generator": "template",
                            "template": "{first}.{last}@example.com",
                        },
                        "first": {"type": "string", "generator": "constant", "value": "a"},
                        "last": {"type": "string", "generator": "constant", "value": "b"},
                    },
                }
            }
        )
        order = compiled.entity("person").field_order
        assert order.index("email") == 2

    def test_orders_entities_by_reference(self) -> None:
        compiled = compile_from(
            {
                "login": {
                    "fields": {
                        "who": {
                            "type": "string",
                            "generator": "reference",
                            "entity": "employee",
                            "on_unavailable": "placeholder",
                        }
                    }
                },
                "employee": {"fields": {"id": {"type": "string", "generator": "sequence"}}},
            }
        )
        assert compiled.entity_order == ("employee", "login")

    def test_relationships_imply_ordering(self) -> None:
        project = make_project(
            {
                "employee": {"fields": {"id": {"type": "string", "generator": "sequence"}}},
                "company": {"fields": {"id": {"type": "string", "generator": "sequence"}}},
            }
        )
        project.relationships.append(
            RelationshipSpec(**{"from": "company", "to": "employee", "cardinality": "one_to_many"})
        )
        assert compile_project(project).entity_order == ("company", "employee")

    def test_field_cycle_is_reported(self) -> None:
        with pytest.raises(CircularDependencyError, match="Circular field dependency"):
            compile_from(
                {
                    "e": {
                        "fields": {
                            "a": {"generator": "template", "template": "{b}"},
                            "b": {"generator": "template", "template": "{a}"},
                        }
                    }
                }
            )

    def test_unknown_dependency_is_reported(self) -> None:
        with pytest.raises(UnknownFieldReferenceError, match="ghost"):
            compile_from({"e": {"fields": {"a": {"generator": "template", "template": "{ghost}"}}}})

    def test_unknown_generator_is_reported(self) -> None:
        with pytest.raises(GeneratorNotFoundError):
            compile_from({"e": {"fields": {"a": {"generator": "telepathy"}}}})

    def test_bad_option_names_the_field(self) -> None:
        with pytest.raises(GeneratorConfigError, match=r"e\.a"):
            compile_from({"e": {"fields": {"a": {"generator": "sequence", "format": "no-token"}}}})

    def test_context_may_name_sibling_fields_or_entities(self) -> None:
        """Section 49's 'Context' list mixes both, so both must resolve."""
        compiled = compile_from(
            {
                "device": {"fields": {"id": {"type": "string", "generator": "sequence"}}},
                "ticket": {
                    "fields": {
                        "category": {"generator": "constant", "value": "VPN"},
                        "summary": {
                            "type": "text",
                            "generator": "llm",
                            "on_unavailable": "placeholder",
                            "context": ["category", "device"],
                        },
                    }
                },
            }
        )
        summary = compiled.entity("ticket").field("summary")
        assert summary.dependencies == ("category",)
        assert summary.related_entities == ("device",)
        assert compiled.entity_order == ("device", "ticket")

    def test_context_typo_is_reported(self) -> None:
        with pytest.raises(SchemaError, match="neither a field"):
            compile_from(
                {
                    "e": {
                        "fields": {
                            "a": {
                                "generator": "llm",
                                "on_unavailable": "null",
                                "context": ["nonsense"],
                            }
                        }
                    }
                }
            )

    def test_empty_project_is_rejected(self) -> None:
        with pytest.raises(SchemaError, match="no entities"):
            compile_from({})

    def test_entity_without_fields_is_rejected(self) -> None:
        with pytest.raises(SchemaError, match="no fields"):
            compile_from({"e": {"count": 5, "fields": {}}})

    def test_inferred_generators_are_flagged(self) -> None:
        compiled = compile_from({"p": {"fields": {"first_name": {"type": "string"}}}})
        field = compiled.entity("p").field("first_name")
        assert field.inferred_generator
        assert field.generator_name == "faker"


class TestPlan:
    def test_plan_covers_every_entity_in_order(self) -> None:
        compiled = compile_from(
            {
                "a": {"count": 10, "fields": {"x": {"generator": "constant", "value": 1}}},
                "b": {"count": 20, "fields": {"y": {"generator": "constant", "value": 2}}},
            }
        )
        plan = compiled.plan
        assert [step.entity for step in plan.steps] == list(compiled.entity_order)
        assert plan.estimate.records == 30
        assert plan.estimate.fields == 30

    def test_llm_calls_are_estimated(self) -> None:
        compiled = compile_from(
            {
                "t": {
                    "count": 100,
                    "fields": {
                        "a": {"generator": "constant", "value": 1},
                        "b": {"type": "text", "generator": "llm", "on_unavailable": "null"},
                    },
                }
            }
        )
        assert compiled.plan.estimate.llm_calls == 100

    def test_plan_renders_and_serialises(self, corporate_project) -> None:
        plan = compile_project(corporate_project).plan
        assert "GENERATION PLAN" in plan.render()
        assert plan.to_dict()["entity_order"]


class TestLinter:
    def _codes(self, entities: dict, **keys) -> set[str]:
        return {issue.code for issue in lint_project(compile_from(entities, **keys))}

    def test_llm_without_semantics(self) -> None:
        codes = self._codes(
            {
                "e": {
                    "fields": {
                        "bio": {"type": "text", "generator": "llm", "on_unavailable": "null"}
                    }
                }
            }
        )
        assert "llm-without-semantics" in codes

    def test_bulk_media_warning(self) -> None:
        codes = self._codes(
            {
                "e": {
                    "count": 500_000,
                    "fields": {"portrait": {"type": "image", "on_unavailable": "null"}},
                }
            }
        )
        assert "bulk-media" in codes

    def test_unrealistic_age_distribution(self) -> None:
        """Design document section 102's worked example."""
        codes = self._codes(
            {
                "e": {
                    "fields": {
                        "age": {"type": "integer", "generator": "random", "min": 18, "max": 90}
                    }
                }
            }
        )
        assert "unrealistic-distribution" in codes

    def test_unique_field_with_too_small_a_domain_is_an_error(self) -> None:
        report = lint_project(
            compile_from(
                {
                    "e": {
                        "count": 100,
                        "fields": {
                            "k": {
                                "generator": "weighted",
                                "choices": ["a", "b"],
                                "unique": True,
                            }
                        },
                    }
                }
            )
        )
        assert not report.ok
        assert any(issue.code == "unique-exhaustion" for issue in report.errors)

    def test_empty_entity(self) -> None:
        assert "empty-entity" in self._codes(
            {"e": {"count": 0, "fields": {"x": {"generator": "constant", "value": 1}}}}
        )

    def test_missing_provider_is_an_error(self) -> None:
        project = make_project(
            {
                "e": {
                    "fields": {
                        "b": {
                            "type": "text",
                            "semantic": "words",
                            "generator": "llm",
                            "provider": "ghost",
                            "on_unavailable": "null",
                        }
                    }
                }
            }
        )
        report = lint_project(compile_project(project))
        assert any(issue.code == "missing-provider" for issue in report.errors)

    def test_clean_schema_has_no_errors(self, corporate_project) -> None:
        report = lint_project(compile_project(corporate_project))
        assert report.ok
        assert report.render()

    def test_severity_ordering_helpers(self) -> None:
        report = lint_project(
            compile_from(
                {
                    "e": {
                        "fields": {
                            "bio": {"type": "text", "generator": "llm", "on_unavailable": "null"}
                        }
                    }
                }
            )
        )
        assert all(issue.severity in Severity for issue in report)


class TestShippedTemplates:
    def test_every_template_compiles(self, template_path) -> None:
        compiled = compile_project(load_project(template_path))
        assert compiled.entities
        assert compiled.plan is not None

    def test_every_template_lints_without_errors(self, template_path) -> None:
        report = lint_project(compile_project(load_project(template_path)))
        assert report.ok, report.render()

    def test_every_template_declares_a_seed(self, template_path) -> None:
        """Reproducibility (section 4) starts with the schema pinning a seed."""
        assert load_project(template_path).project.seed != 0

    def test_no_template_generates_a_real_looking_domain(self, template_path) -> None:
        """Section 62, checked at the schema level as well as the value level."""
        text = template_path.read_text(encoding="utf-8")
        for forbidden in ("@gmail.com", "@outlook.com", "acme.com", "@yahoo."):
            assert forbidden not in text


def test_field_meaning_prefers_semantic_over_description() -> None:
    project = make_project(
        {"e": {"fields": {"f": {"semantic": "the meaning", "description": "the docs"}}}}
    )
    assert project.entities["e"].fields["f"].meaning == "the meaning"


def test_nullable_without_probability_gets_a_default() -> None:
    project = make_project({"e": {"fields": {"f": {"nullable": True}}}})
    assert project.entities["e"].fields["f"].effective_null_probability > 0


def test_entity_lookup_error_lists_known_entities() -> None:
    project = make_project({"e": {"fields": {"f": {"type": "string"}}}})
    with pytest.raises(KeyError, match="Known entities"):
        project.entity("missing")


def test_primary_key_resolution() -> None:
    project = make_project({"e": {"fields": {"k": {"type": "string", "primary_key": True}}}})
    assert project.entities["e"].resolved_primary_key() == "k"


def test_data_type_round_trips_through_yaml(tmp_path) -> None:
    project = make_project({"e": {"fields": {"f": {"type": "ip_address"}}}})
    reloaded = load_project(save_project(project, tmp_path / "p.yaml"))
    assert reloaded.entities["e"].fields["f"].type is DataType.IP_ADDRESS
