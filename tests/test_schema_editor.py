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
