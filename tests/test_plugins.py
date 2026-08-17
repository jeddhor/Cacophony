"""The plugin architecture, and the decision about `script`.

Design document sections 44 and 8.

**The property under test is a negative one**: Cacophony must not load Python
from a project directory. A schema arrives by email, in a Git repository, inside
a `.cacophony` bundle, and if opening one could load its own code then every
other safety property in the platform is decoration — the expression allow-list,
the bundle importer's refusal of traversal, all of it.

So discovery is entry points and only entry points, and that is asserted
directly: a `plugins/` directory beside a schema, full of Python, is ignored.

The rest is a contract in two directions. A plugin that registers something its
manifest did not declare has it refused; one that declares something and never
registers it is reported incomplete. Neither is a security measure — a plugin is
code the user installed — but a manifest that has drifted from its code produces
a project that works on one machine and fails on another with no clue why.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from cacophony.core.errors import GenerationError, SchemaError
from cacophony.generation.engine import GenerationEngine
from cacophony.plugins import (
    CATEGORIES,
    ENTRY_POINT_GROUP,
    PluginError,
    PluginHost,
    PluginManifest,
    PluginRegistry,
    check_requirements,
    load_plugins,
)
from cacophony.schema.compiler import compile_project
from cacophony.schema.loader import load_project, load_project_data
from helpers import compile_from

# --------------------------------------------------------------------------- #
# Fixtures: a plugin, and an entry point that yields it
# --------------------------------------------------------------------------- #


class _FakeEntry:
    """What `importlib.metadata.entry_points` hands back, minus the packaging."""

    def __init__(self, name: str, target: Any) -> None:
        self.name = name
        self._target = target

    def load(self) -> Any:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def demo_generator() -> type:
    from cacophony.core.interfaces import SyncGenerator
    from cacophony.generation.generators.base import OptionsMixin

    class Demo(OptionsMixin, SyncGenerator):
        """A trivial generator; the wiring is what is under test."""

        deterministic = True

        def prepare(self) -> None:
            self.prefix = self.opt_str("prefix", "X") or "X"

        def generate_sync(self, context: Any) -> Any:
            return f"{self.prefix}-{context.record_index}"

        def describe(self) -> str:
            return f"demo({self.prefix})"

    return Demo


def make_plugin(
    name: str = "demo",
    *,
    provides: dict[str, list[str]] | None = None,
    register: Any = None,
    manifest: Any = None,
    replace: bool = False,
) -> Any:
    """A plugin object with whatever manifest and behaviour a test needs."""
    declared = provides if provides is not None else {"generators": [f"{name}_value"]}

    class Plugin:
        pass

    Plugin.manifest = (  # type: ignore[attr-defined]
        manifest if manifest is not None else {"name": name, "version": "1.0", "provides": declared}
    )
    Plugin.replace = replace  # type: ignore[attr-defined]
    Plugin.register = (  # type: ignore[attr-defined]
        register
        if register is not None
        else (lambda self, host: host.add_generator(f"{name}_value", demo_generator()))
    )
    return Plugin()


@pytest.fixture(autouse=True)
def clean_registries():
    """Undo anything a test registered.

    The registries plugins contribute to are process-wide, so a test that leaves
    a generator behind changes what every later test sees.
    """
    from cacophony.generation.registry import REGISTRY as GENERATORS
    from cacophony.outputs import OUTPUT_FORMATS
    from cacophony.providers.registry import PROVIDER_REGISTRY
    from cacophony.scenarios import extra_scenarios
    from cacophony.transforms.operations import OPERATIONS
    from cacophony.validation import extra_validators

    before = {
        "generators": set(GENERATORS.names()),
        "aliases": dict(GENERATORS.aliases()),
        "transforms": set(OPERATIONS),
        "outputs": set(OUTPUT_FORMATS),
        "adapters": set(PROVIDER_REGISTRY.adapters()),
    }
    yield
    for name in set(GENERATORS.names()) - before["generators"]:
        GENERATORS.unregister(name)
    for name in set(OPERATIONS) - before["transforms"]:
        del OPERATIONS[name]
    for name in set(OUTPUT_FORMATS) - before["outputs"]:
        del OUTPUT_FORMATS[name]
    extra_validators().clear()
    extra_scenarios().clear()
    load_plugins(force=True, entries=[])


def load(*plugins: Any) -> PluginRegistry:
    return load_plugins(
        force=True,
        entries=[_FakeEntry(getattr(p, "manifest", {}).get("name", "p"), p) for p in plugins],
    )


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #


class TestManifest:
    def test_section_44s_eight_categories(self) -> None:
        assert set(CATEGORIES) == {
            "generators",
            "validators",
            "transforms",
            "outputs",
            "language_models",
            "images",
            "speech",
            "scenarios",
        }

    def test_section_44s_own_example_parses(self) -> None:
        manifest = PluginManifest.from_dict(
            {
                "name": "My Custom Generator",
                # YAML reads `1.0` as a float; refusing that would be pedantry
                # about the design document's own example.
                "version": 1.0,
                "provides": {"generators": ["network_packet_generator"]},
            }
        )
        assert manifest.name == "My Custom Generator"
        assert manifest.version == "1.0"
        assert manifest.provides == {"generators": ["network_packet_generator"]}

    def test_a_name_is_required(self) -> None:
        with pytest.raises(PluginError, match="needs a 'name'"):
            PluginManifest.from_dict({"provides": {"generators": ["x"]}})

    def test_declaring_nothing_is_refused(self) -> None:
        """A plugin that provides nothing cannot be checked against its code."""
        with pytest.raises(PluginError, match="declares nothing"):
            PluginManifest.from_dict({"name": "empty", "provides": {}})

    def test_an_unknown_category_is_refused_with_the_list(self) -> None:
        with pytest.raises(PluginError, match="unknown plugin category"):
            PluginManifest.from_dict({"name": "odd", "provides": {"widgets": ["x"]}})

    def test_a_single_name_may_be_a_string(self) -> None:
        manifest = PluginManifest.from_dict({"name": "one", "provides": {"generators": "solo"}})
        assert manifest.provides["generators"] == ["solo"]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


class TestLoading:
    def test_a_plugin_registers_its_generator(self) -> None:
        from cacophony.generation.registry import REGISTRY

        registry = load(make_plugin("demo"))
        assert registry.names() == ["demo"]
        assert registry.working
        assert "demo_value" in REGISTRY.names()

    def test_a_registered_generator_is_usable_in_a_project(self) -> None:
        load(make_plugin("demo"))
        compiled = compile_from(
            {
                "row": {
                    "count": 5,
                    "fields": {"value": {"generator": "demo_value", "prefix": "Q"}},
                }
            }
        )
        records = asyncio.run(GenerationEngine(compiled).generate_batch("row", 3))
        assert [record.values["value"] for record in records] == ["Q-0", "Q-1", "Q-2"]

    def test_an_instance_entry_point_is_used_as_is(self) -> None:
        assert load(make_plugin("demo")).working

    def test_a_class_entry_point_is_instantiated(self) -> None:
        """Which is what a package usually exposes."""
        from cacophony.generation.registry import REGISTRY

        registry = load_plugins(
            force=True, entries=[_FakeEntry("cls", type(make_plugin("classy")))]
        )
        assert registry.working, registry.broken[0].error if registry.broken else None
        assert "classy_value" in REGISTRY.names()

    def test_all_eight_categories_reach_a_registry(self) -> None:
        """Section 44 lists eight; each must actually arrive somewhere."""
        from cacophony.outputs import OUTPUT_FORMATS
        from cacophony.providers.registry import PROVIDER_REGISTRY
        from cacophony.scenarios import extra_scenarios
        from cacophony.transforms.operations import OPERATIONS
        from cacophony.validation import extra_validators

        def register(self: Any, host: PluginHost) -> None:
            host.add_generator("p_gen", demo_generator())
            host.add_transform("p_transform", lambda value, _arg: str(value)[::-1])
            host.add_output("p_output", type("W", (), {}))
            host.add_validator("p_validator", type("V", (), {}))
            host.add_scenario("p_scenario", type("S", (), {}))
            host.add_language_model("p_llm", type("L", (), {}))
            host.add_image_provider("p_image", type("I", (), {}))
            host.add_speech_provider("p_speech", type("T", (), {}))

        registry = load(
            make_plugin(
                "everything",
                provides={
                    "generators": ["p_gen"],
                    "transforms": ["p_transform"],
                    "outputs": ["p_output"],
                    "validators": ["p_validator"],
                    "scenarios": ["p_scenario"],
                    "language_models": ["p_llm"],
                    "images": ["p_image"],
                    "speech": ["p_speech"],
                },
                register=register,
            )
        )
        manifest = registry.by_name("everything")
        assert manifest is not None and manifest.ok, manifest.to_dict() if manifest else None

        from cacophony.generation.registry import REGISTRY as GENERATORS

        assert "p_gen" in GENERATORS.names()
        assert "p_transform" in OPERATIONS
        assert "p_output" in OUTPUT_FORMATS
        assert "p_validator" in extra_validators()
        assert "p_scenario" in extra_scenarios()
        for adapter in ("p_llm", "p_image", "p_speech"):
            assert adapter in PROVIDER_REGISTRY.adapters()

    def test_a_transform_from_a_plugin_works_in_a_patch_rule(self) -> None:
        """A TransformPlugin reaches section 105's operations."""
        from cacophony.transforms import PatchRule, apply_operations, parse_step

        load(
            make_plugin(
                "reverser",
                provides={"transforms": ["reverse"]},
                register=lambda self, host: host.add_transform(
                    "reverse", lambda value, _arg: str(value)[::-1]
                ),
            )
        )
        assert apply_operations("abc", [parse_step("reverse")]) == "cba"
        rule = PatchRule.parse("r", {"set": {"name": "reverse"}})
        assert rule.edits[0].apply({"name": "amara"}) == "arama"


class TestLoadingFailures:
    def test_a_plugin_that_cannot_import_is_recorded_not_raised(self) -> None:
        """A broken plugin installed last month must not stop today's run."""
        registry = load_plugins(
            force=True, entries=[_FakeEntry("broken", ImportError("no such module"))]
        )
        assert registry.broken
        assert "could not import" in registry.broken[0].error

    def test_a_plugin_whose_register_raises_is_recorded(self) -> None:
        def explode(self: Any, host: PluginHost) -> None:
            raise RuntimeError("the disk is on fire")

        registry = load(make_plugin("bad", register=explode))
        assert "register() failed" in registry.broken[0].error

    def test_a_plugin_with_no_manifest_is_refused(self) -> None:
        class Bare:
            def register(self, host: PluginHost) -> None:
                pass

        registry = load_plugins(force=True, entries=[_FakeEntry("bare", Bare())])
        assert "needs a 'manifest'" in registry.broken[0].error

    def test_a_working_plugin_still_loads_beside_a_broken_one(self) -> None:
        registry = load_plugins(
            force=True,
            entries=[
                _FakeEntry("broken", ImportError("nope")),
                _FakeEntry("demo", make_plugin("demo")),
            ],
        )
        assert len(registry.broken) == 1
        assert len(registry.working) == 1


class TestTheManifestIsAContract:
    def test_an_undeclared_contribution_is_refused(self) -> None:
        from cacophony.generation.registry import REGISTRY

        registry = load(
            make_plugin(
                "sneaky",
                provides={"generators": ["declared"]},
                register=lambda self, host: (
                    host.add_generator("declared", demo_generator()),
                    host.add_generator("undeclared", demo_generator()),
                ),
            )
        )
        manifest = registry.by_name("sneaky")
        assert manifest is not None
        assert manifest.refused == ["generators.undeclared"]
        assert not manifest.ok
        assert "undeclared" not in REGISTRY.names()
        assert "declared" in REGISTRY.names()

    def test_a_declared_but_unregistered_name_is_reported(self) -> None:
        registry = load(
            make_plugin(
                "forgetful",
                provides={"generators": ["one", "two"]},
                register=lambda self, host: host.add_generator("one", demo_generator()),
            )
        )
        manifest = registry.by_name("forgetful")
        assert manifest is not None
        assert manifest.missing == ["generators.two"]
        assert not manifest.ok

    def test_a_plugin_cannot_silently_replace_a_built_in(self) -> None:
        """One that took over `uuid` would change every project on the machine."""
        registry = load(
            make_plugin(
                "hostile",
                provides={"generators": ["uuid"]},
                register=lambda self, host: host.add_generator("uuid", demo_generator()),
            )
        )
        assert "already registered" in registry.broken[0].error

    def test_replace_is_possible_when_declared(self) -> None:
        from cacophony.generation.registry import REGISTRY

        original = REGISTRY.get("uuid")
        registry = load(
            make_plugin(
                "deliberate",
                provides={"generators": ["uuid"]},
                register=lambda self, host: host.add_generator("uuid", demo_generator()),
                replace=True,
            )
        )
        assert registry.working
        assert REGISTRY.get("uuid") is not original
        # Put it back, since the autouse fixture only removes additions.
        REGISTRY.register("uuid", original, replace=True)

    def test_contributions_are_attributable(self) -> None:
        registry = load(make_plugin("demo"))
        assert registry.contributions()["generators"]["demo_value"] == "demo"


# --------------------------------------------------------------------------- #
# The property that matters
# --------------------------------------------------------------------------- #


class TestCodeIsNeverLoadedFromAProject:
    """The decision the whole phase turns on.

    A schema is something people share. If opening one could load its own Python,
    every other safety property in the platform would be decoration.
    """

    def test_a_plugins_directory_beside_a_schema_is_ignored(self, tmp_path: Path) -> None:
        marker = tmp_path / "EXECUTED"
        (tmp_path / "plugins").mkdir()
        (tmp_path / "plugins" / "evil.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('owned')\n",
            encoding="utf-8",
        )
        # And the two other names somebody might expect to be magic.
        for directory in ("cacophony_plugins", "extensions"):
            (tmp_path / directory).mkdir()
            (tmp_path / directory / "evil.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('owned')\n",
                encoding="utf-8",
            )

        project = tmp_path / "project.yaml"
        project.write_text(
            yaml.safe_dump(
                {
                    "project": {"name": "Innocent"},
                    "entities": {"row": {"count": 2, "fields": {"a": {"generator": "uuid"}}}},
                }
            ),
            encoding="utf-8",
        )

        compiled = compile_project(load_project(project))
        asyncio.run(GenerationEngine(compiled).generate_batch("row", 2))

        assert not marker.exists(), "code from a project directory was executed"

    def test_discovery_reads_only_the_entry_point_group(self) -> None:
        assert ENTRY_POINT_GROUP == "cacophony.plugins"
        source = Path("backend/cacophony/plugins/registry.py").read_text(encoding="utf-8")
        # No directory scanning of any kind in the loader.
        for forbidden in (
            "glob(",
            "rglob(",
            "iterdir(",
            "listdir",
            "importlib.util.spec_from_file",
        ):
            assert forbidden not in source, f"the loader reaches the filesystem: {forbidden}"

    def test_loading_can_be_switched_off_entirely(self, monkeypatch) -> None:
        monkeypatch.setenv("CACOPHONY_NO_PLUGINS", "1")
        registry = load_plugins(force=True, entries=[_FakeEntry("demo", make_plugin("demo"))])
        assert registry.disabled
        assert not registry.manifests


# --------------------------------------------------------------------------- #
# requires:
# --------------------------------------------------------------------------- #


class TestRequires:
    def test_a_project_may_require_a_plugin(self) -> None:
        load(make_plugin("demo"))
        project = load_project_data(
            {
                "project": {"name": "Needs"},
                "requires": {"plugins": ["demo"]},
                "entities": {"row": {"count": 2, "fields": {"v": {"generator": "demo_value"}}}},
            }
        )
        assert compile_project(project) is not None

    def test_an_absent_plugin_is_refused_with_what_to_install(self) -> None:
        load(make_plugin("demo"))
        with pytest.raises(SchemaError, match="requires plugin"):
            check_requirements(["nowhere"])

    def test_the_message_names_what_is_installed(self) -> None:
        load(make_plugin("demo"))
        with pytest.raises(SchemaError, match="Installed: demo"):
            check_requirements(["nowhere"])

    def test_the_message_says_where_plugins_come_from(self) -> None:
        """Because the next question is always "so where do I put it"."""
        load()
        with pytest.raises(SchemaError, match=ENTRY_POINT_GROUP):
            check_requirements(["nowhere"])

    def test_a_broken_required_plugin_is_refused(self) -> None:
        registry = load(
            make_plugin(
                "half",
                provides={"generators": ["one", "two"]},
                register=lambda self, host: host.add_generator("one", demo_generator()),
            )
        )
        with pytest.raises(SchemaError, match="did not load cleanly"):
            check_requirements(["half"], registry=registry)

    def test_requiring_with_loading_disabled_is_refused(self, monkeypatch) -> None:
        monkeypatch.setenv("CACOPHONY_NO_PLUGINS", "1")
        load_plugins(force=True, entries=[])
        with pytest.raises(SchemaError, match="CACOPHONY_NO_PLUGINS"):
            check_requirements(["demo"])

    def test_requiring_nothing_costs_nothing(self) -> None:
        check_requirements([])

    def test_the_compiler_checks_requirements(self) -> None:
        load()
        with pytest.raises(SchemaError, match="requires plugin"):
            compile_project(
                load_project_data(
                    {
                        "project": {"name": "Needs"},
                        "requires": {"plugins": ["absent"]},
                        "entities": {"row": {"count": 1, "fields": {"a": {"generator": "uuid"}}}},
                    }
                )
            )


# --------------------------------------------------------------------------- #
# Section 8's script generator
# --------------------------------------------------------------------------- #


class TestScriptStaysRefused:
    """A decision, not a postponement.

    The plugin phase measured what isolation is available and concluded that a
    trustworthy sandbox is not affordable here. Unprivileged namespaces block
    the network on Linux but leave the filesystem readable; blocking that needs
    mount namespaces, which do not exist on the two other platforms the desktop
    phase targets. A restricted interpreter is a denylist, not a sandbox.
    """

    def test_a_script_field_still_compiles(self) -> None:
        """So a project written today survives a future phase that ships it."""
        compiled = compile_from(
            {"row": {"count": 2, "fields": {"x": {"generator": "script", "code": "return 1"}}}}
        )
        assert compiled.entity("row").fields[0].generator_name == "script"

    def test_running_it_is_refused_as_a_decision(self) -> None:
        compiled = compile_from(
            {"row": {"count": 2, "fields": {"x": {"generator": "script", "code": "return 1"}}}}
        )
        with pytest.raises(GenerationError, match="deliberately not implemented"):
            asyncio.run(GenerationEngine(compiled).generate_batch("row", 1))

    def test_the_refusal_points_at_the_three_alternatives(self) -> None:
        compiled = compile_from(
            {"row": {"count": 2, "fields": {"x": {"generator": "script", "code": "return 1"}}}}
        )
        with pytest.raises(GenerationError) as caught:
            asyncio.run(GenerationEngine(compiled).generate_batch("row", 1))
        message = str(caught.value)
        assert "expression" in message
        assert "patches" in message
        assert "plugin" in message

    def test_the_location_appears_once(self) -> None:
        """It read `row.x: row.x: ...` until the engine stopped double-prefixing."""
        compiled = compile_from(
            {"row": {"count": 2, "fields": {"x": {"generator": "script", "code": "return 1"}}}}
        )
        with pytest.raises(GenerationError) as caught:
            asyncio.run(GenerationEngine(compiled).generate_batch("row", 1))
        assert str(caught.value).count("row.x:") == 1

    def test_a_placeholder_runs_the_pipeline(self) -> None:
        compiled = compile_from(
            {
                "row": {
                    "count": 3,
                    "fields": {
                        "x": {
                            "generator": "script",
                            "code": "return 1",
                            "on_unavailable": "placeholder",
                        }
                    },
                }
            }
        )
        records = asyncio.run(GenerationEngine(compiled).generate_batch("row", 3))
        assert all("script" in str(record.values["x"]) for record in records)


# --------------------------------------------------------------------------- #
# The CLI and the API
# --------------------------------------------------------------------------- #


class TestCli:
    def test_it_explains_where_plugins_come_from_when_none_are_installed(self, monkeypatch) -> None:
        """Asserted against an empty environment rather than this machine's.

        The CLI reloads from real entry points, so a test that assumed nothing
        was installed would pass or fail depending on what the developer had
        pip-installed that morning.
        """
        from typer.testing import CliRunner

        from cacophony.cli.main import app
        from cacophony.plugins import registry as registry_module

        monkeypatch.setattr(registry_module, "_entry_points", lambda: [])
        result = CliRunner().invoke(app, ["plugins"])
        assert result.exit_code == 0
        assert ENTRY_POINT_GROUP in result.stdout
        assert "does not load Python from a project directory" in result.stdout

    def test_it_lists_an_installed_plugin(self) -> None:
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        # The CLI reloads from real entry points, so this asserts on the shape
        # rather than on the fake plugin above.
        result = CliRunner().invoke(app, ["plugins", "--json"])
        assert result.exit_code in (0, 1)
        import json

        payload = json.loads(result.stdout)
        assert payload["entry_point_group"] == ENTRY_POINT_GROUP
        assert isinstance(payload["plugins"], list)

    def test_showing_an_unknown_plugin_lists_what_there_is(self) -> None:
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        result = CliRunner().invoke(app, ["plugins", "--show", "nowhere"])
        assert result.exit_code == 2
        assert "no plugin named" in result.stderr


class TestApi:
    def test_the_route_reports_the_categories_and_the_group(self) -> None:
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from cacophony.api.app import create_app
        from cacophony.api.service import RunService

        with TestClient(create_app(service=RunService(store_path=":memory:"))) as client:
            payload = client.get("/api/plugins").json()

        assert payload["entry_point_group"] == ENTRY_POINT_GROUP
        assert set(payload["categories"]) == set(CATEGORIES)
