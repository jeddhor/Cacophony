"""Comment-preserving schema editing (design document sections 48, 74).

The point of these is not that an edit works — it is that everything the edit
did *not* touch survives it. A Studio that silently strips a documented
schema's comments is a Studio nobody uses twice.
"""

from __future__ import annotations

import pytest
import yaml

from cacophony.core.errors import SchemaError
from cacophony.schema.editor import EditOperation, apply_patch, describe_operations
from cacophony.schema.loader import load_project_data

DOCUMENTED = """\
# Corporate Directory
# Employees, departments and the laptops they are issued.

project:
  name: Corporate Directory
  seed: 42069        # pinned so runs reproduce

entities:

  # ---------------------------------------------------------------- #
  employee:
    count: 5000
    primary_key: employee_id

    fields:
      employee_id:
        type: string
        generator: sequence
        format: "EMP-{000000}"

      # No generator named: the recommendation engine picks one.
      first_name:
        type: string
        semantic: "Person's given name"

      department:
        type: enum
        generator: weighted
        choices:
          Engineering: 40
          Sales: 60

  device:
    count: 100
    fields:
      asset_tag:
        type: string
        generator: sequence
"""


def parsed(source: str) -> dict:
    return yaml.safe_load(source)


class TestPreservation:
    def test_comments_survive_an_edit(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [{"op": "set_entity", "entity": "employee", "key": "count", "value": 9999}],
        )
        assert "# Corporate Directory" in result.source
        assert "# Employees, departments" in result.source
        assert "# No generator named" in result.source
        assert "# pinned so runs reproduce" in result.source
        assert parsed(result.source)["entities"]["employee"]["count"] == 9999

    def test_untouched_lines_are_byte_identical(self) -> None:
        """Only the changed scalar should differ, so a diff stays readable."""
        result = apply_patch(
            DOCUMENTED,
            [{"op": "set_entity", "entity": "device", "key": "count", "value": 250}],
        )
        before = DOCUMENTED.splitlines()
        after = result.source.splitlines()
        changed = [
            (left, right) for left, right in zip(before, after, strict=False) if left != right
        ]
        assert len(changed) == 1
        assert "250" in changed[0][1]

    def test_field_order_is_kept(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [
                {
                    "op": "set_field",
                    "entity": "employee",
                    "field": "first_name",
                    "key": "unique",
                    "value": True,
                }
            ],
        )
        fields = parsed(result.source)["entities"]["employee"]["fields"]
        assert list(fields) == ["employee_id", "first_name", "department"]

    def test_quoted_strings_stay_quoted(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [{"op": "set_entity", "entity": "employee", "key": "count", "value": 10}],
        )
        assert '"EMP-{000000}"' in result.source


class TestOperations:
    def test_set_project(self) -> None:
        result = apply_patch(DOCUMENTED, [{"op": "set_project", "key": "seed", "value": 7}])
        assert parsed(result.source)["project"]["seed"] == 7

    def test_set_field(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [
                {
                    "op": "set_field",
                    "entity": "employee",
                    "field": "first_name",
                    "key": "semantic",
                    "value": "Given name",
                }
            ],
        )
        field = parsed(result.source)["entities"]["employee"]["fields"]["first_name"]
        assert field["semantic"] == "Given name"

    def test_setting_none_removes_the_key(self) -> None:
        """Clearing a control in a form should delete the key, not write null."""
        result = apply_patch(
            DOCUMENTED,
            [
                {
                    "op": "set_field",
                    "entity": "employee",
                    "field": "first_name",
                    "key": "semantic",
                    "value": None,
                }
            ],
        )
        assert (
            "semantic" not in parsed(result.source)["entities"]["employee"]["fields"]["first_name"]
        )

    def test_unset_field(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [
                {
                    "op": "unset_field",
                    "entity": "employee",
                    "field": "employee_id",
                    "key": "format",
                }
            ],
        )
        assert (
            "format" not in parsed(result.source)["entities"]["employee"]["fields"]["employee_id"]
        )

    def test_add_field(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [
                {
                    "op": "add_field",
                    "entity": "employee",
                    "name": "nickname",
                    "value": {"type": "string", "generator": "faker", "provider": "first_name"},
                }
            ],
        )
        fields = parsed(result.source)["entities"]["employee"]["fields"]
        assert fields["nickname"]["generator"] == "faker"
        assert list(fields)[-1] == "nickname"

    def test_add_field_at_an_index(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [{"op": "add_field", "entity": "employee", "name": "prefix", "index": 0}],
        )
        fields = parsed(result.source)["entities"]["employee"]["fields"]
        assert next(iter(fields)) == "prefix"

    def test_remove_field(self) -> None:
        result = apply_patch(
            DOCUMENTED, [{"op": "remove_field", "entity": "employee", "name": "department"}]
        )
        assert "department" not in parsed(result.source)["entities"]["employee"]["fields"]

    def test_rename_field_keeps_its_position(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [
                {
                    "op": "rename_field",
                    "entity": "employee",
                    "field": "first_name",
                    "name": "given_name",
                }
            ],
        )
        fields = list(parsed(result.source)["entities"]["employee"]["fields"])
        assert fields == ["employee_id", "given_name", "department"]

    def test_move_field(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [{"op": "move_field", "entity": "employee", "name": "department", "index": 0}],
        )
        fields = list(parsed(result.source)["entities"]["employee"]["fields"])
        assert fields == ["department", "employee_id", "first_name"]

    def test_add_entity_compiles_immediately(self) -> None:
        """A new entity with no fields would not compile, so it starts with one."""
        result = apply_patch(DOCUMENTED, [{"op": "add_entity", "name": "location"}])
        entity = parsed(result.source)["entities"]["location"]
        assert entity["count"] == 100
        assert list(entity["fields"]) == ["id"]

    def test_remove_entity(self) -> None:
        result = apply_patch(DOCUMENTED, [{"op": "remove_entity", "name": "device"}])
        assert "device" not in parsed(result.source)["entities"]

    def test_several_operations_apply_in_order(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [
                {"op": "add_field", "entity": "device", "name": "hostname"},
                {
                    "op": "set_field",
                    "entity": "device",
                    "field": "hostname",
                    "key": "generator",
                    "value": "pattern",
                },
                {
                    "op": "set_field",
                    "entity": "device",
                    "field": "hostname",
                    "key": "pattern",
                    "value": "wks-{0000}",
                },
            ],
        )
        field = parsed(result.source)["entities"]["device"]["fields"]["hostname"]
        assert field["generator"] == "pattern"
        assert field["pattern"] == "wks-{0000}"
        assert len(result.applied) == 3


class TestRefusals:
    def test_an_edit_that_would_not_compile_is_refused(self) -> None:
        with pytest.raises(SchemaError):
            apply_patch(
                DOCUMENTED,
                [
                    {
                        "op": "set_field",
                        "entity": "employee",
                        "field": "first_name",
                        "key": "type",
                        "value": "banana",
                    }
                ],
            )

    def test_a_patch_is_all_or_nothing(self) -> None:
        """The valid half of a bad patch must not reach the document."""
        with pytest.raises(SchemaError):
            apply_patch(
                DOCUMENTED,
                [
                    {"op": "set_entity", "entity": "employee", "key": "count", "value": 1},
                    {"op": "set_entity", "entity": "ghost", "key": "count", "value": 1},
                ],
            )

    def test_an_unknown_operation_lists_the_known_ones(self) -> None:
        with pytest.raises(SchemaError, match="Available"):
            apply_patch(DOCUMENTED, [{"op": "levitate"}])

    def test_an_unknown_entity_is_named(self) -> None:
        with pytest.raises(SchemaError, match="ghost"):
            apply_patch(
                DOCUMENTED, [{"op": "set_entity", "entity": "ghost", "key": "count", "value": 1}]
            )

    def test_an_unknown_field_lists_the_real_ones(self) -> None:
        with pytest.raises(SchemaError, match="employee_id"):
            apply_patch(
                DOCUMENTED,
                [
                    {
                        "op": "set_field",
                        "entity": "employee",
                        "field": "ghost",
                        "key": "type",
                        "value": "string",
                    }
                ],
            )

    def test_the_last_field_cannot_be_removed(self) -> None:
        with pytest.raises(SchemaError, match="at least one field"):
            apply_patch(
                DOCUMENTED, [{"op": "remove_field", "entity": "device", "name": "asset_tag"}]
            )

    def test_the_last_entity_cannot_be_removed(self) -> None:
        source = apply_patch(DOCUMENTED, [{"op": "remove_entity", "name": "device"}]).source
        with pytest.raises(SchemaError, match="at least one entity"):
            apply_patch(source, [{"op": "remove_entity", "name": "employee"}])

    def test_a_duplicate_field_name_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="already exists"):
            apply_patch(
                DOCUMENTED, [{"op": "add_field", "entity": "employee", "name": "department"}]
            )

    def test_broken_yaml_is_reported_as_such(self) -> None:
        with pytest.raises(SchemaError, match="not valid YAML"):
            apply_patch("project: [unclosed\n", [{"op": "set_project", "key": "seed", "value": 1}])

    def test_an_empty_patch_changes_nothing(self) -> None:
        result = apply_patch(DOCUMENTED, [])
        assert result.source == DOCUMENTED
        assert result.changed is False


def test_the_edited_schema_still_loads() -> None:
    result = apply_patch(
        DOCUMENTED,
        [
            {"op": "set_entity", "entity": "employee", "key": "count", "value": 12},
            {"op": "add_field", "entity": "employee", "name": "nickname"},
        ],
    )
    project = load_project_data(parsed(result.source))
    assert project.entities["employee"].count == 12
    assert "nickname" in project.entities["employee"].fields


def test_operations_are_documented_for_the_studio() -> None:
    described = {entry["op"] for entry in describe_operations()}
    assert "set_field" in described and "rename_field" in described


def test_operation_from_dict_rejects_nonsense() -> None:
    with pytest.raises(SchemaError):
        EditOperation.from_dict({"op": "nope"})


class TestChaosBlock:
    """What the Chaos Panel writes (sections 24, 78)."""

    def test_setting_a_rate_creates_the_block(self) -> None:
        result = apply_patch(DOCUMENTED, [{"op": "set_chaos", "key": "outliers", "value": 0.05}])
        assert parsed(result.source)["chaos"] == {"outliers": 0.05}

    def test_a_preset_and_a_rate_live_together(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [
                {"op": "set_chaos", "key": "preset", "value": "messy"},
                {"op": "set_chaos", "key": "duplicates", "value": 0.02},
            ],
        )
        assert parsed(result.source)["chaos"] == {"preset": "messy", "duplicates": 0.02}

    def test_clearing_the_last_key_removes_the_block(self) -> None:
        """`chaos:` with nothing under it says less than no block at all."""
        with_chaos = apply_patch(
            DOCUMENTED, [{"op": "set_chaos", "key": "outliers", "value": 0.05}]
        ).source
        cleared = apply_patch(with_chaos, [{"op": "set_chaos", "key": "outliers", "value": None}])
        assert "chaos" not in parsed(cleared.source)

    def test_an_impossible_rate_is_refused(self) -> None:
        with pytest.raises(SchemaError):
            apply_patch(DOCUMENTED, [{"op": "set_chaos", "key": "outliers", "value": 5.0}])

    def test_the_operation_is_documented(self) -> None:
        assert any(entry["op"] == "set_chaos" for entry in describe_operations())


class TestProviders:
    """Configuring a backend from the Studio (sections 43, 63, 85).

    A provider is four lines of YAML, which is exactly the kind of thing people
    get wrong once and then avoid. Editing it through the same patch mechanism
    means the surrounding document survives, and that a mistake is refused
    before it reaches the file rather than at run time.
    """

    WITH_PROVIDER = (
        DOCUMENTED
        + """
providers:

  # The local Ollama server.
  local_llm:
    adapter: ollama
    base_url: http://localhost:11434
    model: llama3.1:8b
"""
    )

    def test_add_provider(self) -> None:
        result = apply_patch(
            DOCUMENTED,
            [
                {
                    "op": "add_provider",
                    "name": "local_llm",
                    "value": {
                        "adapter": "ollama",
                        "base_url": "http://localhost:11434",
                        "model": "llama3.1:8b",
                        "concurrency": 4,
                    },
                }
            ],
        )
        provider = parsed(result.source)["providers"]["local_llm"]
        assert provider == {
            "adapter": "ollama",
            "base_url": "http://localhost:11434",
            "model": "llama3.1:8b",
            "concurrency": 4,
        }
        # First key, because it decides what the rest mean.
        assert next(iter(provider)) == "adapter"

    def test_set_provider_leaves_the_comment_alone(self) -> None:
        result = apply_patch(
            self.WITH_PROVIDER,
            [{"op": "set_provider", "name": "local_llm", "key": "concurrency", "value": 8}],
        )
        assert parsed(result.source)["providers"]["local_llm"]["concurrency"] == 8
        assert "# The local Ollama server." in result.source

    def test_clearing_a_key_removes_it(self) -> None:
        result = apply_patch(
            self.WITH_PROVIDER,
            [{"op": "set_provider", "name": "local_llm", "key": "model", "value": None}],
        )
        assert "model" not in parsed(result.source)["providers"]["local_llm"]

    def test_remove_provider(self) -> None:
        """The last one takes the empty block with it, or the file stops loading."""
        result = apply_patch(self.WITH_PROVIDER, [{"op": "remove_provider", "name": "local_llm"}])
        assert "providers" not in parsed(result.source)
        assert load_project_data(parsed(result.source)).providers == {}

    def test_an_unknown_adapter_is_refused_with_the_list(self) -> None:
        """A misspelled adapter compiles, and then quietly produces nothing."""
        with pytest.raises(SchemaError, match="Available adapters"):
            apply_patch(
                DOCUMENTED,
                [{"op": "add_provider", "name": "local_llm", "value": {"adapter": "olama"}}],
            )
        with pytest.raises(SchemaError, match="Available adapters"):
            apply_patch(
                self.WITH_PROVIDER,
                [{"op": "set_provider", "name": "local_llm", "key": "adapter", "value": "olama"}],
            )

    def test_a_provider_in_use_is_not_removed_from_under_its_fields(self) -> None:
        source = apply_patch(
            self.WITH_PROVIDER,
            [
                {
                    "op": "add_field",
                    "entity": "employee",
                    "name": "biography",
                    "value": {
                        "type": "text",
                        "semantic": "A short professional biography.",
                        "generator": "llm",
                        "provider": "local_llm",
                    },
                }
            ],
        ).source

        with pytest.raises(SchemaError, match=r"employee\.biography"):
            apply_patch(source, [{"op": "remove_provider", "name": "local_llm"}])

    def test_a_credential_pasted_into_the_secret_field_is_refused(self) -> None:
        """Section 63: project files hold secret ids, never secrets."""
        with pytest.raises(SchemaError, match="logical secret id"):
            apply_patch(
                self.WITH_PROVIDER,
                [
                    {
                        "op": "set_provider",
                        "name": "local_llm",
                        "key": "secret",
                        "value": "sk-notarealkeybutlongenoughtolooklikeone",
                    }
                ],
            )

    def test_an_unknown_provider_is_named(self) -> None:
        with pytest.raises(SchemaError, match="Configured providers"):
            apply_patch(
                self.WITH_PROVIDER,
                [{"op": "set_provider", "name": "ghost", "key": "model", "value": "x"}],
            )
