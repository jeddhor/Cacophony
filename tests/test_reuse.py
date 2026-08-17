"""Reuse: recipes, the built-in catalogue, and bundles.

Design document sections 80, 106 and 72.

    recipes   a named fragment of schema, inserted with one line
    catalogue thirty of them, shipped
    bundles   a project that can be sent to somebody else

Three properties carry most of these tests.

**Expansion is visible.** A schema that silently gains eight fields is a schema
nobody can debug, so every expanded field records the recipe it came from, and
overriding one field of a recipe leaves the rest of it alone and in place.

**Every catalogue recipe compiles and generates.** A catalogue is a promise, and
a recipe that fails to compile is worse than no recipe: somebody reaches for it
expecting to save time and loses an afternoon instead. Each of the thirty is
expanded into a project and run.

**A bundle from somebody else is untrusted input.** Path traversal, absolute
paths, symlinks and Windows drive letters are all refused, before anything is
written - so an archive with one bad entry leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from cacophony.core.errors import SchemaError
from cacophony.generation.engine import GenerationEngine
from cacophony.schema.bundle import (
    BUNDLE_SUFFIX,
    MANIFEST_NAME,
    BundleManifest,
    export_bundle,
    import_bundle,
    inspect_bundle,
)
from cacophony.schema.compiler import compile_project
from cacophony.schema.loader import load_project, load_project_data
from cacophony.schema.recipes import (
    Recipe,
    RecipeLibrary,
    expand_recipes,
    load_library,
)

# --------------------------------------------------------------------------- #
# Section 80 - expansion
# --------------------------------------------------------------------------- #


def library_with(**recipes: dict[str, Any]) -> RecipeLibrary:
    library = RecipeLibrary()
    for name, data in recipes.items():
        library.add(Recipe.from_dict(name, data, source="test"))
    return library


class TestRecipeModel:
    def test_a_recipe_needs_fields_or_includes(self) -> None:
        with pytest.raises(SchemaError, match="defines no fields"):
            Recipe.from_dict("empty", {"description": "nothing"})

    def test_a_recipe_must_be_a_mapping(self) -> None:
        with pytest.raises(SchemaError, match="must be a mapping"):
            Recipe.from_dict("odd", ["not", "a", "mapping"])

    def test_a_field_must_be_a_mapping(self) -> None:
        with pytest.raises(SchemaError, match="field 'x' must be a mapping"):
            Recipe.from_dict("odd", {"fields": {"x": "sequence"}})

    def test_an_unknown_recipe_lists_what_is_available(self) -> None:
        library = library_with(one={"fields": {"a": {"generator": "uuid"}}})
        with pytest.raises(SchemaError, match="Available: one"):
            library.get("two")


class TestResolution:
    def test_includes_are_expanded_first(self) -> None:
        library = library_with(
            base={"fields": {"a": {"generator": "uuid"}}},
            derived={"includes": ["base"], "fields": {"b": {"generator": "uuid"}}},
        )
        assert list(library.resolve("derived")) == ["a", "b"]

    def test_a_recipe_can_override_a_field_of_what_it_includes(self) -> None:
        library = library_with(
            base={"fields": {"a": {"type": "string", "generator": "uuid"}}},
            derived={"includes": ["base"], "fields": {"a": {"generator": "sequence"}}},
        )
        resolved = library.resolve("derived")
        assert resolved["a"]["generator"] == "sequence"
        # A field-level key survives; the generator's options do not.
        assert resolved["a"]["type"] == "string"

    def test_attribution_names_what_was_asked_for(self) -> None:
        """Somebody who wrote `recipes: [employee]` wants to be told "employee"."""
        library = library_with(
            base={"fields": {"a": {"generator": "uuid"}}},
            derived={"includes": ["base"], "fields": {"b": {"generator": "uuid"}}},
        )
        resolved = library.resolve("derived")
        assert resolved["a"]["recipe"] == "base"
        assert resolved["b"]["recipe"] == "derived"

    def test_a_cycle_is_reported_as_a_cycle(self) -> None:
        library = library_with(
            one={"includes": ["two"], "fields": {"a": {"generator": "uuid"}}},
            two={"includes": ["one"], "fields": {"b": {"generator": "uuid"}}},
        )
        with pytest.raises(SchemaError, match="cycle: one -> two -> one"):
            library.resolve("one")

    def test_groups_are_sorted_and_complete(self) -> None:
        library = load_library()
        grouped = library.groups()
        assert set(grouped) == {"identity", "computing", "security", "commerce", "operational"}
        assert sum(len(entries) for entries in grouped.values()) == len(library)


class TestExpansion:
    def _expand(self, entity: dict[str, Any], library: RecipeLibrary) -> dict[str, Any]:
        data = {"project": {"name": "T"}, "entities": {"thing": entity}}
        return expand_recipes(data, library=library)["entities"]["thing"]

    def test_a_recipe_becomes_ordinary_fields(self) -> None:
        library = library_with(
            two={"fields": {"a": {"generator": "uuid"}, "b": {"generator": "uuid"}}}
        )
        expanded = self._expand({"count": 5, "recipes": ["two"]}, library)
        assert "recipes" not in expanded
        assert list(expanded["fields"]) == ["a", "b"]

    def test_the_entity_keeps_its_own_fields_after_the_recipe(self) -> None:
        library = library_with(one={"fields": {"a": {"generator": "uuid"}}})
        expanded = self._expand(
            {"recipes": ["one"], "fields": {"z": {"generator": "uuid"}}}, library
        )
        assert list(expanded["fields"]) == ["a", "z"]

    def test_an_override_stays_where_the_recipe_put_it(self) -> None:
        """Or overriding one template reorders the record."""
        library = library_with(
            three={
                "fields": {
                    "a": {"generator": "uuid"},
                    "b": {"generator": "template", "template": "x"},
                    "c": {"generator": "uuid"},
                }
            }
        )
        expanded = self._expand({"recipes": ["three"], "fields": {"b": {"template": "y"}}}, library)
        assert list(expanded["fields"]) == ["a", "b", "c"]
        assert expanded["fields"]["b"]["template"] == "y"
        assert expanded["fields"]["b"]["generator"] == "template"

    def test_an_override_is_still_attributed(self) -> None:
        library = library_with(one={"fields": {"a": {"generator": "template", "template": "x"}}})
        expanded = self._expand({"recipes": ["one"], "fields": {"a": {"template": "y"}}}, library)
        assert expanded["fields"]["a"]["recipe"] == "one"

    def test_naming_a_different_generator_drops_the_old_options(self) -> None:
        """Or a template's options sit in a language model's option bag."""
        library = library_with(
            one={
                "fields": {
                    "a": {
                        "type": "text",
                        "generator": "template",
                        "template": "{x} {y}",
                        "on_missing": "empty",
                    }
                }
            }
        )
        expanded = self._expand(
            {"recipes": ["one"], "fields": {"a": {"generator": "llm", "semantic": "prose"}}},
            library,
        )
        field = expanded["fields"]["a"]
        assert field["generator"] == "llm"
        assert "template" not in field
        assert "on_missing" not in field
        # Field-level keys are not generator options and survive.
        assert field["type"] == "text"

    def test_self_substitutes_the_entity_being_expanded_into(self) -> None:
        """Section 80's manager relationship, which a recipe cannot name."""
        library = library_with(
            hierarchy={"fields": {"parent": {"generator": "reference", "entity": "$self"}}}
        )
        expanded = self._expand({"recipes": ["hierarchy"]}, library)
        assert expanded["fields"]["parent"]["entity"] == "thing"

    def test_several_recipes_merge_in_order(self) -> None:
        library = library_with(
            first={"fields": {"a": {"generator": "uuid"}}},
            second={"fields": {"a": {"generator": "sequence"}, "b": {"generator": "uuid"}}},
        )
        expanded = self._expand({"recipes": ["first", "second"]}, library)
        assert expanded["fields"]["a"]["generator"] == "sequence"
        assert list(expanded["fields"]) == ["a", "b"]

    def test_a_project_with_no_recipes_is_returned_untouched(self) -> None:
        """A project that uses none should not pay to read the catalogue."""
        data = {"project": {"name": "T"}, "entities": {"thing": {"count": 1, "fields": {}}}}
        assert expand_recipes(data) is data

    def test_recipes_must_be_a_list(self) -> None:
        library = library_with(one={"fields": {"a": {"generator": "uuid"}}})
        with pytest.raises(SchemaError, match="must be a list"):
            self._expand({"recipes": {"one": True}}, library)

    def test_a_string_is_accepted_as_one_recipe(self) -> None:
        library = library_with(one={"fields": {"a": {"generator": "uuid"}}})
        assert list(self._expand({"recipes": "one"}, library)["fields"]) == ["a"]


class TestProjectLocalRecipes:
    def test_an_inline_definition_is_usable(self) -> None:
        project = load_project_data(
            {
                "project": {"name": "Inline"},
                "recipes": {
                    "tiny": {
                        "description": "One field.",
                        "fields": {"code": {"type": "string", "generator": "uuid"}},
                    }
                },
                "entities": {"thing": {"count": 2, "recipes": ["tiny"]}},
            }
        )
        assert project.entity("thing").field_names() == ["code"]
        assert project.entity("thing").fields["code"].recipe == "tiny"

    def test_a_project_may_replace_a_built_in(self) -> None:
        """Precedence matters when the catalogue is nearly right."""
        project = load_project_data(
            {
                "project": {"name": "Override"},
                "recipes": {
                    "email": {"fields": {"email": {"type": "string", "generator": "uuid"}}}
                },
                "entities": {"thing": {"count": 2, "recipes": ["email"]}},
            }
        )
        assert project.entity("thing").fields["email"].generator is not None
        assert project.entity("thing").fields["email"].generator.type == "uuid"

    def test_a_recipes_directory_beside_the_project_is_found(self, tmp_path: Path) -> None:
        (tmp_path / "recipes").mkdir()
        (tmp_path / "recipes" / "local.yaml").write_text(
            yaml.safe_dump(
                {
                    "group": "local",
                    "badge": {"fields": {"badge": {"type": "string", "generator": "uuid"}}},
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "project.yaml").write_text(
            yaml.safe_dump(
                {
                    "project": {"name": "Local"},
                    "entities": {"thing": {"count": 2, "recipes": ["badge"]}},
                }
            ),
            encoding="utf-8",
        )
        project = load_project(tmp_path / "project.yaml")
        assert project.entity("thing").field_names() == ["badge"]


# --------------------------------------------------------------------------- #
# Section 106 - the catalogue
# --------------------------------------------------------------------------- #


CATALOGUE = load_library()


class TestCatalogue:
    def test_section_106_names_are_all_present(self) -> None:
        """The lists in section 106, checked name by name."""
        expected = {
            "identity": {"person", "employee", "customer", "username", "email", "address"},
            "computing": {
                "hostname",
                "ip",
                "mac",
                "os",
                "browser",
                "device",
                "software_version",
            },
            "security": {
                "cvss_score",
                "cve_identifier",
                "alert_severity",
                "logon_event",
                "network_event",
                "hash_value",
            },
            "commerce": {"product", "sku", "transaction", "invoice", "price", "currency"},
            "operational": {"ticket", "status", "priority", "comment", "timestamp"},
        }
        grouped = CATALOGUE.groups()
        for group, names in expected.items():
            present = {recipe.name for recipe in grouped[group]}
            assert names <= present, f"{group} is missing {names - present}"

    def test_every_recipe_describes_itself(self) -> None:
        for name in CATALOGUE.names():
            recipe = CATALOGUE.get(name)
            assert recipe.description, f"{name} has no description"
            assert recipe.group != "custom", f"{name} has no group"

    @pytest.mark.parametrize("name", CATALOGUE.names())
    def test_every_recipe_compiles_and_generates(self, name: str) -> None:
        """A catalogue is a promise. A recipe that does not work breaks it."""
        project = load_project_data(
            {
                "project": {"name": f"Recipe {name}", "seed": 4242},
                "timeline": {"start": "2026-01-01", "end": "2026-06-30"},
                "entities": {"thing": {"count": 30, "recipes": [name]}},
            }
        )
        compiled = compile_project(project)
        records = asyncio.run(GenerationEngine(compiled).generate_batch("thing", 30))

        assert len(records) == 30
        expected = set(CATALOGUE.resolve(name))
        for record in records:
            assert set(record.values) == expected

    @pytest.mark.parametrize("name", CATALOGUE.names())
    def test_every_recipe_passes_its_own_validation(self, name: str) -> None:
        """Including uniqueness: a recipe claiming `unique` must deliver it.

        `username` was written with `unique: true` and could not honour it -
        two J. Smiths collide - which would have failed the first real run.
        """
        from cacophony.validation.pipeline import RecordValidator

        project = load_project_data(
            {
                "project": {"name": f"Recipe {name}", "seed": 909},
                "timeline": {"start": "2026-01-01", "end": "2026-06-30"},
                "entities": {"thing": {"count": 300, "recipes": [name]}},
            }
        )
        compiled = compile_project(project)
        engine = GenerationEngine(compiled)
        records = asyncio.run(engine.generate_batch("thing", 300))

        validator = RecordValidator(compiled.entity("thing"))
        failures = [
            issue.render() for record in records for issue in validator.validate(record).errors
        ]
        assert not failures, failures[:4]

    def test_the_addresses_are_all_documentation_ranges(self) -> None:
        """Section 62, checked on what the catalogue actually produces."""
        import ipaddress

        safe = [
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("198.51.100.0/24"),
            ipaddress.ip_network("203.0.113.0/24"),
        ]
        project = load_project_data(
            {
                "project": {"name": "Addresses", "seed": 7},
                "entities": {
                    "thing": {"count": 40, "recipes": ["ip", "mac", "hostname", "network_event"]}
                },
            }
        )
        compiled = compile_project(project)
        records = asyncio.run(GenerationEngine(compiled).generate_batch("thing", 40))

        for record in records:
            for key, value in record.values.items():
                if "ip" in key and value:
                    address = ipaddress.ip_address(str(value))
                    assert any(address in network for network in safe), f"{key}={value}"
                if key == "mac_address" and value:
                    assert str(value).lower().startswith("00:00:5e")
                if key == "hostname" and value:
                    assert str(value).endswith(".example")

    def test_the_cve_identifiers_are_unique_and_shaped_right(self) -> None:
        """A random five-digit pattern would collide; this one is derived."""
        project = load_project_data(
            {
                "project": {"name": "CVEs", "seed": 11},
                "entities": {"thing": {"count": 800, "recipes": ["cve_identifier"]}},
            }
        )
        compiled = compile_project(project)
        records = asyncio.run(GenerationEngine(compiled).generate_batch("thing", 800))
        ids = [record.values["cve_id"] for record in records]
        assert len(set(ids)) == 800
        assert all(value.startswith("CVE-2026-") for value in ids)

    def test_invoice_arithmetic_adds_up_in_decimals(self) -> None:
        from decimal import Decimal

        project = load_project_data(
            {
                "project": {"name": "Invoices", "seed": 13},
                "entities": {"thing": {"count": 50, "recipes": ["invoice"]}},
            }
        )
        compiled = compile_project(project)
        records = asyncio.run(GenerationEngine(compiled).generate_batch("thing", 50))
        for record in records:
            net = record.values["net_amount"]
            tax = record.values["tax_amount"]
            gross = record.values["gross_amount"]
            assert isinstance(net, Decimal)
            assert gross == net + tax


class TestSectionEightyExample:
    """Section 80's own example, end to end."""

    def test_one_line_becomes_the_fields_it_lists(self) -> None:
        project = load_project_data(
            {
                "project": {"name": "Section 80", "seed": 8080},
                "entities": {"employee": {"count": 200, "recipes": ["employee"]}},
            }
        )
        names = project.entity("employee").field_names()
        # first name, last name, email, username, employee ID, manager.
        for wanted in ("first_name", "last_name", "email", "username", "employee_id", "manager"):
            assert wanted in names

    def test_the_manager_chain_is_acyclic(self) -> None:
        """A self-reference points backwards, or no query over it terminates."""
        project = load_project_data(
            {
                "project": {"name": "Section 80", "seed": 8080},
                "entities": {"employee": {"count": 300, "recipes": ["employee"]}},
            }
        )
        compiled = compile_project(project)
        records = asyncio.run(GenerationEngine(compiled).generate_batch("employee", 300))

        for record in records:
            manager = record.values["manager"]
            if manager is not None:
                assert manager < record.values["employee_id"]

    def test_the_first_record_has_no_manager(self) -> None:
        """The top of a hierarchy has no parent, which is correct."""
        project = load_project_data(
            {
                "project": {"name": "Section 80", "seed": 8080},
                "entities": {"employee": {"count": 20, "recipes": ["employee"]}},
            }
        )
        compiled = compile_project(project)
        records = asyncio.run(GenerationEngine(compiled).generate_batch("employee", 20))
        assert records[0].values["manager"] is None


# --------------------------------------------------------------------------- #
# Project-relative paths
# --------------------------------------------------------------------------- #


class TestProjectRelativePaths:
    """A relative path means relative to the schema, not to the shell.

    Without this, a project can only be used from the directory it lives in -
    and a portable bundle is impossible, because the only paths that work are
    absolute ones, which are exactly the paths that do not travel.
    """

    def _project(self, tmp_path: Path) -> Path:
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "codes.csv").write_text("code\nAA\nBB\nCC\n", encoding="utf-8")
        path = tmp_path / "project.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "project": {"name": "Relative"},
                    "entities": {
                        "thing": {
                            "count": 5,
                            "fields": {
                                "code": {
                                    "type": "string",
                                    "generator": "lookup",
                                    "path": "data/codes.csv",
                                    "column": "code",
                                }
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_it_compiles_from_anywhere(self, tmp_path: Path, monkeypatch) -> None:
        path = self._project(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        compiled = compile_project(load_project(path))
        records = asyncio.run(GenerationEngine(compiled).generate_batch("thing", 5))
        assert {record.values["code"] for record in records} <= {"AA", "BB", "CC"}

    def test_the_base_directory_never_reaches_the_document(self, tmp_path: Path) -> None:
        """It describes this machine, and a schema is shared."""
        from cacophony.schema.loader import dump_project

        project = load_project(self._project(tmp_path))
        assert project.base_dir == tmp_path.resolve()
        assert "base_dir" not in dump_project(project)


# --------------------------------------------------------------------------- #
# Section 72 - bundles
# --------------------------------------------------------------------------- #


def make_bundle_source(root: Path, *, absolute: Path | None = None) -> Path:
    """A small project with a local recipe and a data file."""
    (root / "recipes").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "data" / "codes.csv").write_text("code\nCC-1\nCC-2\n", encoding="utf-8")
    (root / "templates" / "note.txt").write_text("a template\n", encoding="utf-8")
    (root / "recipes" / "local.yaml").write_text(
        yaml.safe_dump(
            {
                "group": "local",
                "coded": {
                    "description": "A code from our own table.",
                    "fields": {
                        "code": {
                            "type": "string",
                            "generator": "lookup",
                            "path": str(absolute) if absolute else "data/codes.csv",
                            "column": "code",
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    path = root / "project.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "project": {"name": "Team Fixture", "description": "Shared.", "seed": 72},
                "entities": {"employee": {"count": 40, "recipes": ["person", "coded"]}},
            }
        ),
        encoding="utf-8",
    )
    return path


class TestBundleExport:
    def test_it_writes_an_archive_with_a_manifest(self, tmp_path: Path) -> None:
        source = make_bundle_source(tmp_path / "src")
        path, manifest = export_bundle(source, tmp_path / "team")

        assert path.suffix == BUNDLE_SUFFIX
        assert manifest.project == "Team Fixture"
        assert manifest.entities == {"employee": 40}
        assert set(manifest.recipes_used) == {"person", "coded"}

        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        assert MANIFEST_NAME in names
        assert "project.yaml" in names
        assert "recipes/local.yaml" in names
        assert "templates/note.txt" in names

    def test_it_packs_files_the_schema_references(self, tmp_path: Path) -> None:
        """Found by testing: the first bundle went out without its own CSV.

        The path lives in the recipe file, not the project, so collecting
        references from the authored document alone missed it.
        """
        source = make_bundle_source(tmp_path / "src")
        path, _manifest = export_bundle(source, tmp_path / "team")
        with zipfile.ZipFile(path) as archive:
            assert "data/codes.csv" in archive.namelist()

    def test_generated_data_is_never_packed(self, tmp_path: Path) -> None:
        """Section 72 is explicit about this."""
        root = tmp_path / "src"
        source = make_bundle_source(root)
        (root / "templates" / "leftover.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
        (root / "templates" / "old.parquet").write_bytes(b"PAR1")

        path, _manifest = export_bundle(source, tmp_path / "team")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        assert not any(name.endswith((".jsonl", ".parquet")) for name in names)

    def test_a_path_inside_the_project_is_rewritten(self, tmp_path: Path) -> None:
        root = tmp_path / "src"
        source = make_bundle_source(root)
        # Point the project itself at an absolute path inside its own directory.
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
        data["entities"]["employee"]["fields"] = {
            "code2": {
                "type": "string",
                "generator": "lookup",
                "path": str(root / "data" / "codes.csv"),
                "column": "code",
            }
        }
        source.write_text(yaml.safe_dump(data), encoding="utf-8")

        path, manifest = export_bundle(source, tmp_path / "team")
        with zipfile.ZipFile(path) as archive:
            packed = yaml.safe_load(archive.read("project.yaml"))
        assert packed["entities"]["employee"]["fields"]["code2"]["path"] == "data/codes.csv"
        assert any("rewrote" in note for note in manifest.notes)

    def test_a_path_outside_the_project_is_refused(self, tmp_path: Path) -> None:
        """Silently dropping it would produce a bundle that fails on record one."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "codes.csv").write_text("code\nZZ\n", encoding="utf-8")
        source = make_bundle_source(tmp_path / "src", absolute=outside / "codes.csv")

        with pytest.raises(SchemaError, match="outside the project directory"):
            export_bundle(source, tmp_path / "team")

    def test_an_existing_bundle_is_not_replaced_silently(self, tmp_path: Path) -> None:
        source = make_bundle_source(tmp_path / "src")
        target = tmp_path / "team.cacophony"
        export_bundle(source, target)
        with pytest.raises(SchemaError, match="already exists"):
            export_bundle(source, target)
        export_bundle(source, target, overwrite=True)


class TestBundleInspect:
    def test_it_verifies_and_compiles(self, tmp_path: Path) -> None:
        source = make_bundle_source(tmp_path / "src")
        path, _manifest = export_bundle(source, tmp_path / "team")

        report = inspect_bundle(path)
        assert report.ok
        assert report.project_ok, report.project_error
        assert not report.tampered
        assert not report.missing

    def test_it_compiles_the_bundle_as_a_whole(self, tmp_path: Path) -> None:
        """Found by testing: compiling project.yaml alone reported a broken
        bundle that was fine, because the recipe it needs is a sibling file."""
        source = make_bundle_source(tmp_path / "src")
        path, _manifest = export_bundle(source, tmp_path / "team")
        report = inspect_bundle(path)
        assert report.project_ok
        assert "recipes/local.yaml" in report.entries

    def test_it_writes_nothing(self, tmp_path: Path) -> None:
        source = make_bundle_source(tmp_path / "src")
        path, _manifest = export_bundle(source, tmp_path / "team")
        before = sorted(item.name for item in tmp_path.iterdir())
        inspect_bundle(path)
        assert sorted(item.name for item in tmp_path.iterdir()) == before

    def test_tampering_is_detected(self, tmp_path: Path) -> None:
        source = make_bundle_source(tmp_path / "src")
        path, _manifest = export_bundle(source, tmp_path / "team")

        # Rewrite one entry, leaving the manifest's hash for it in place.
        rebuilt = tmp_path / "rebuilt.cacophony"
        with zipfile.ZipFile(path) as original, zipfile.ZipFile(rebuilt, "w") as copy:
            for info in original.infolist():
                data = original.read(info)
                if info.filename == "data/codes.csv":
                    data = b"code\nEVIL\n"
                copy.writestr(info.filename, data)

        report = inspect_bundle(rebuilt)
        assert report.tampered == ["data/codes.csv"]
        assert not report.ok

    def test_a_non_bundle_is_refused(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain.cacophony"
        with zipfile.ZipFile(plain, "w") as archive:
            archive.writestr("hello.txt", "hi")
        with pytest.raises(SchemaError, match="not a Cacophony bundle"):
            inspect_bundle(plain)

    def test_a_corrupt_archive_is_refused(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.cacophony"
        broken.write_bytes(b"not a zip at all")
        with pytest.raises(SchemaError, match="not a readable zip"):
            inspect_bundle(broken)

    def test_a_future_format_is_refused_rather_than_misread(self) -> None:
        with pytest.raises(SchemaError, match="format version"):
            BundleManifest.from_dict({"format_version": 99})


class TestBundleImport:
    def test_a_round_trip_still_generates(self, tmp_path: Path) -> None:
        source = make_bundle_source(tmp_path / "src")
        path, _manifest = export_bundle(source, tmp_path / "team")

        target, report = import_bundle(path, tmp_path / "unpacked")
        assert report.project_ok

        compiled = compile_project(load_project(target / "project.yaml"))
        records = asyncio.run(GenerationEngine(compiled).generate_batch("employee", 20))
        assert len(records) == 20
        assert {record.values["code"] for record in records} <= {"CC-1", "CC-2"}

    def test_it_refuses_a_directory_that_is_not_empty(self, tmp_path: Path) -> None:
        source = make_bundle_source(tmp_path / "src")
        path, _manifest = export_bundle(source, tmp_path / "team")
        busy = tmp_path / "busy"
        busy.mkdir()
        (busy / "mine.txt").write_text("keep me", encoding="utf-8")

        with pytest.raises(SchemaError, match="not empty"):
            import_bundle(path, busy)
        assert (busy / "mine.txt").read_text() == "keep me"

        import_bundle(path, busy, overwrite=True)
        assert (busy / "project.yaml").exists()


class TestBundleSafety:
    """An archive from somebody else is untrusted input."""

    def _hostile(self, tmp_path: Path, name: str, *, symlink: bool = False) -> Path:
        path = tmp_path / "hostile.cacophony"
        manifest = {"format_version": 1, "project": "Hostile", "files": {}}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest))
            archive.writestr("project.yaml", "project:\n  name: Hostile\nentities: {}\n")
            if symlink:
                info = zipfile.ZipInfo(name)
                info.external_attr = 0o120777 << 16
                archive.writestr(info, "/etc/passwd")
            else:
                archive.writestr(name, "owned")
        return path

    @pytest.mark.parametrize(
        "name",
        [
            "../../.bashrc",
            "../escape.txt",
            "a/b/../../../../etc/passwd",
            "/etc/absolute",
            "C:/Windows/System32/evil",
        ],
    )
    def test_a_path_that_escapes_is_refused(self, tmp_path: Path, name: str) -> None:
        path = self._hostile(tmp_path, name)
        target = tmp_path / "out"
        with pytest.raises(SchemaError, match="refusing"):
            import_bundle(path, target)

    def test_a_symlink_entry_is_refused(self, tmp_path: Path) -> None:
        path = self._hostile(tmp_path, "link", symlink=True)
        with pytest.raises(SchemaError, match="not a regular file"):
            import_bundle(path, tmp_path / "out")

    def test_nothing_is_written_before_the_whole_archive_is_checked(self, tmp_path: Path) -> None:
        """One bad entry must leave nothing behind, not half a project."""
        path = tmp_path / "mixed.cacophony"
        manifest = {"format_version": 1, "project": "Mixed", "files": {}}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest))
            archive.writestr("project.yaml", "project:\n  name: Mixed\nentities: {}\n")
            archive.writestr("harmless.txt", "fine")
            archive.writestr("../escape.txt", "owned")

        target = tmp_path / "out"
        with pytest.raises(SchemaError, match="refusing"):
            import_bundle(path, target)
        assert not target.exists() or not any(target.iterdir())
        assert not (tmp_path / "escape.txt").exists()

    def test_an_absurdly_large_bundle_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        from cacophony.schema import bundle as bundle_module

        monkeypatch.setattr(bundle_module, "MAX_TOTAL_BYTES", 16)
        source = make_bundle_source(tmp_path / "src")
        path, _manifest = export_bundle(source, tmp_path / "team")
        with pytest.raises(SchemaError, match="above the"):
            import_bundle(path, tmp_path / "out")

    def test_too_many_entries_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        from cacophony.schema import bundle as bundle_module

        monkeypatch.setattr(bundle_module, "MAX_ENTRIES", 2)
        source = make_bundle_source(tmp_path / "src")
        path, _manifest = export_bundle(source, tmp_path / "team")
        with pytest.raises(SchemaError, match="above the"):
            import_bundle(path, tmp_path / "out")
