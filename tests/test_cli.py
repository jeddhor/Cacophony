"""The command-line interface, end to end (design document sections 37 and 88).

Section 88 asks for integration tests covering
``schema -> generator -> validation -> export``. Driving the CLI is the
cheapest way to cover that whole path, because it is the same path a user
takes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cacophony.cli.main import app
from helpers import TEMPLATES

runner = CliRunner()

SIMPLE_YAML = """
project:
  name: CLI Test
  seed: 4242

entities:
  widget:
    count: 20
    primary_key: sku
    fields:
      sku:
        type: string
        generator: sequence
        format: "SKU-{0000}"
        unique: true
      colour:
        type: enum
        generator: weighted
        choices: [red, green, blue]
      price:
        type: decimal
        generator: random
        min: 1
        max: 100
        precision: 2
      label:
        type: string
        generator: template
        template: "{colour|title} widget {sku}"
"""


@pytest.fixture
def project_file(tmp_path: Path) -> Path:
    path = tmp_path / "project.yaml"
    path.write_text(SIMPLE_YAML, encoding="utf-8")
    return path


def invoke(*args: str):
    return runner.invoke(app, list(args))


class TestValidate:
    def test_valid_project_exits_zero(self, project_file: Path) -> None:
        result = invoke("validate", str(project_file))
        assert result.exit_code == 0
        assert "schema is valid" in result.stdout

    def test_invalid_project_exits_two(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("project:\n  name: x\nentities: {}\n", encoding="utf-8")
        assert invoke("validate", str(path)).exit_code == 2

    def test_missing_file_exits_two(self, tmp_path: Path) -> None:
        assert invoke("validate", str(tmp_path / "nope.yaml")).exit_code == 2

    def test_seed_can_be_overridden(self, project_file: Path) -> None:
        assert invoke("validate", str(project_file), "--seed", "7").exit_code == 0


class TestPlanAndLint:
    def test_plan_lists_every_field_and_its_generator(self, project_file: Path) -> None:
        result = invoke("plan", str(project_file))
        assert result.exit_code == 0
        for expected in ("widget", "sku", "sequence", "weighted", "template"):
            assert expected in result.stdout

    def test_plan_as_json(self, project_file: Path) -> None:
        result = invoke("plan", str(project_file), "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["entity_order"] == ["widget"]
        assert payload["estimate"]["records"] == 20

    def test_lint_clean_project(self, project_file: Path) -> None:
        result = invoke("lint", str(project_file))
        assert result.exit_code == 0

    def test_lint_exits_one_on_errors(self, tmp_path: Path) -> None:
        path = tmp_path / "dupes.yaml"
        path.write_text(
            """
project:
  name: Dupes
  seed: 1
entities:
  e:
    count: 100
    fields:
      k:
        type: string
        unique: true
        generator: weighted
        choices: [a, b]
""",
            encoding="utf-8",
        )
        result = invoke("lint", str(path))
        assert result.exit_code == 1
        assert "unique-exhaustion" in result.stdout


class TestPreview:
    def test_default_preview(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "5")
        assert result.exit_code == 0
        assert "widget" in result.stdout

    def test_json_preview_is_parseable(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "3", "--json")
        assert result.exit_code == 0
        rows = [json.loads(line) for line in result.stdout.strip().splitlines()]
        assert len(rows) == 3
        assert rows[0]["sku"] == "SKU-0001"
        assert rows[0]["label"].endswith("SKU-0001")

    def test_preview_is_faithful_to_a_real_run(self, project_file: Path, tmp_path: Path) -> None:
        """What preview shows is what generate writes."""
        previewed = [
            json.loads(line)
            for line in invoke("preview", str(project_file), "-n", "5", "--json")
            .stdout.strip()
            .splitlines()
        ]
        out = tmp_path / "out"
        invoke("generate", str(project_file), "-o", "jsonl", "-d", str(out))
        written = [
            json.loads(line)
            for line in (out / "widget.jsonl").read_text(encoding="utf-8").strip().splitlines()
        ]
        assert previewed == written[:5]

    def test_isolate_gives_a_different_sample(self, project_file: Path) -> None:
        faithful = invoke("preview", str(project_file), "-n", "3", "--json").stdout
        isolated = invoke("preview", str(project_file), "-n", "3", "--json", "--isolate").stdout
        assert faithful != isolated

    def test_column_selection(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "2", "-c", "sku,colour")
        assert result.exit_code == 0
        assert "price" not in result.stdout

    def test_offset(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "1", "--offset", "9", "--json")
        assert json.loads(result.stdout.strip())["sku"] == "SKU-0010"


class TestGenerate:
    @pytest.mark.parametrize("fmt", ["jsonl", "json", "csv", "parquet"])
    def test_every_format_writes_a_file(self, project_file: Path, tmp_path: Path, fmt: str) -> None:
        out = tmp_path / fmt
        result = invoke("generate", str(project_file), "-o", fmt, "-d", str(out))
        assert result.exit_code == 0, result.stdout
        written = list(out.glob("widget.*"))
        assert len(written) == 1 and written[0].stat().st_size > 0
        assert "complete" in result.stdout

    def test_record_count_override(self, project_file: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        invoke("generate", str(project_file), "-n", "7", "-d", str(out))
        lines = (out / "widget.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 7

    def test_output_is_reproducible(self, project_file: Path, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        invoke("generate", str(project_file), "-d", str(first))
        invoke("generate", str(project_file), "-d", str(second))
        assert (first / "widget.jsonl").read_bytes() == (second / "widget.jsonl").read_bytes()

    def test_seed_changes_the_output(self, project_file: Path, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        invoke("generate", str(project_file), "-d", str(first), "--seed", "1")
        invoke("generate", str(project_file), "-d", str(second), "--seed", "2")
        assert (first / "widget.jsonl").read_bytes() != (second / "widget.jsonl").read_bytes()

    def test_unknown_format_exits_two(self, project_file: Path, tmp_path: Path) -> None:
        result = invoke("generate", str(project_file), "-o", "papyrus", "-d", str(tmp_path))
        assert result.exit_code == 2
        # Errors belong on stderr so `cacophony preview --json | jq` stays usable.
        assert "Choose one of" in result.stderr

    def test_unknown_entity_exits_two(self, project_file: Path, tmp_path: Path) -> None:
        result = invoke("generate", str(project_file), "-e", "ghost", "-d", str(tmp_path))
        assert result.exit_code == 2

    def test_unknown_provenance_mode_exits_two(self, project_file: Path, tmp_path: Path) -> None:
        result = invoke(
            "generate", str(project_file), "--provenance", "telepathy", "-d", str(tmp_path)
        )
        assert result.exit_code == 2

    def test_provenance_reaches_the_file(self, project_file: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        invoke("generate", str(project_file), "-d", str(out), "--provenance", "field")
        first = json.loads((out / "widget.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert first["_provenance"]["fields"]["sku"]["generator"] == "sequence"

    def test_single_entity_selection(self, project_file: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        invoke("generate", str(project_file), "-e", "widget", "-d", str(out))
        assert (out / "widget.jsonl").exists()

    def test_generation_failure_exits_three(self, tmp_path: Path) -> None:
        path = tmp_path / "boom.yaml"
        path.write_text(
            """
project:
  name: Boom
  seed: 1
entities:
  e:
    count: 3
    fields:
      words:
        type: text
        semantic: Some prose
        generator: llm
""",
            encoding="utf-8",
        )
        result = invoke("generate", str(path), "-d", str(tmp_path / "out"))
        assert result.exit_code == 3
        assert "language model" in result.stderr


class TestStreamSeparation:
    def test_errors_go_to_stderr_not_stdout(self, tmp_path: Path) -> None:
        """`cacophony preview --json | jq` must never receive an error message."""
        result = invoke("validate", str(tmp_path / "missing.yaml"))
        assert result.exit_code == 2
        assert result.stdout == ""
        assert "error" in result.stderr

    def test_json_preview_stdout_is_pure_json(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "2", "--json")
        for line in result.stdout.strip().splitlines():
            json.loads(line)


class TestInformationalCommands:
    def test_generators_lists_the_registry(self) -> None:
        result = invoke("generators")
        assert result.exit_code == 0
        for name in ("sequence", "weighted", "faker", "expression"):
            assert name in result.stdout

    def test_generators_as_json(self) -> None:
        rows = json.loads(invoke("generators", "--json").stdout)
        assert any(row["name"] == "sequence" for row in rows)
        assert all("summary" in row for row in rows)

    def test_providers_without_a_project(self) -> None:
        assert invoke("providers").exit_code == 0

    def test_providers_with_a_project(self) -> None:
        result = invoke("providers", str(TEMPLATES / "helpdesk.yaml"))
        assert result.exit_code == 0
        assert "local_llm" in result.stdout

    def test_version(self) -> None:
        from cacophony import __version__

        result = invoke("version")
        assert result.exit_code == 0 and __version__ in result.stdout

    def test_no_arguments_shows_help(self) -> None:
        result = invoke()
        assert "validate" in result.stdout and "generate" in result.stdout


class TestProviderCommands:
    """Commands added with the provider layer (design document sections 12, 36)."""

    MOCK_YAML = """
project:
  name: Mock LLM
  seed: 11
providers:
  assistant:
    type: language_model
    adapter: mock
    model: mock-1
entities:
  note:
    count: 6
    fields:
      note_id:
        type: string
        generator: sequence
        format: "N-{0000}"
      topic:
        type: enum
        generator: weighted
        choices: [billing, access, hardware]
      body:
        type: text
        semantic: A short internal note about the topic.
        generator: llm
        provider: assistant
        context: [topic]
        constraints:
          max_length: 200
"""

    @pytest.fixture
    def mock_project(self, tmp_path: Path) -> Path:
        path = tmp_path / "mock.yaml"
        path.write_text(self.MOCK_YAML, encoding="utf-8")
        return path

    def test_providers_lists_the_secret_id_not_the_secret(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.yaml"
        path.write_text(
            self.MOCK_YAML.replace("    model: mock-1", "    model: mock-1\n    secret: my-key"),
            encoding="utf-8",
        )
        result = invoke("providers", str(path))
        assert result.exit_code == 0
        assert "my-key" in result.stdout

    def test_providers_test_probes_health(self, mock_project: Path) -> None:
        result = invoke("providers", str(mock_project), "--test")
        assert result.exit_code == 0
        assert "ok" in result.stdout

    def test_providers_test_exits_one_when_a_provider_is_down(self, tmp_path: Path) -> None:
        path = tmp_path / "down.yaml"
        path.write_text(
            self.MOCK_YAML.replace(
                "    model: mock-1", "    model: mock-1\n    options:\n      healthy: false"
            ),
            encoding="utf-8",
        )
        result = invoke("providers", str(path), "--test")
        assert result.exit_code == 1
        assert "down" in result.stdout

    def test_models_lists_what_the_provider_serves(self, mock_project: Path) -> None:
        result = invoke("models", str(mock_project))
        assert result.exit_code == 0
        assert "mock-1" in result.stdout

    def test_models_rejects_an_unknown_provider(self, mock_project: Path) -> None:
        result = invoke("models", str(mock_project), "--provider", "ghost")
        assert result.exit_code == 2

    def test_models_needs_a_project_with_providers(self, project_file: Path) -> None:
        result = invoke("models", str(project_file))
        assert result.exit_code == 2

    def test_prompt_shows_the_compiled_instruction(self, mock_project: Path) -> None:
        """Section 9: users should rarely engineer prompts, but must be able to read them."""
        result = invoke("prompt", str(mock_project))
        assert result.exit_code == 0
        assert "SYSTEM" in result.stdout and "USER" in result.stdout
        assert "STRICT JSON" in result.stdout
        assert "A short internal note about the topic." in result.stdout

    def test_prompt_can_show_the_schema(self, mock_project: Path) -> None:
        result = invoke("prompt", str(mock_project), "--schema")
        assert "JSON SCHEMA" in result.stdout
        assert "maxLength" in result.stdout

    def test_prompt_on_a_project_without_ai_fields(self, project_file: Path) -> None:
        result = invoke("prompt", str(project_file))
        assert result.exit_code == 0
        assert "no language-model fields" in result.stdout

    def test_generate_reports_provider_activity(self, mock_project: Path, tmp_path: Path) -> None:
        result = invoke("generate", str(mock_project), "-d", str(tmp_path / "out"))
        assert result.exit_code == 0, result.stdout
        assert "language-model calls" in result.stdout

    def test_generated_records_carry_model_written_text(
        self, mock_project: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        invoke("generate", str(mock_project), "-d", str(out))
        rows = [
            json.loads(line)
            for line in (out / "note.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 6
        assert all(row["body"] for row in rows)
        assert all(len(row["body"]) <= 200 for row in rows)

    def test_cache_makes_a_second_run_free(self, mock_project: Path, tmp_path: Path) -> None:
        """Section 76: identical requests should not be paid for twice."""
        cache = tmp_path / "cache.db"
        first = invoke(
            "generate",
            str(mock_project),
            "-d",
            str(tmp_path / "a"),
            "--cache",
            "read_write",
            "--cache-path",
            str(cache),
        )
        assert first.exit_code == 0
        assert "0 hits" in first.stdout

        second = invoke(
            "generate",
            str(mock_project),
            "-d",
            str(tmp_path / "b"),
            "--cache",
            "read_write",
            "--cache-path",
            str(cache),
        )
        assert second.exit_code == 0
        assert "6 hits" in second.stdout
        # And the data is identical, not merely as numerous.
        assert (tmp_path / "a" / "note.jsonl").read_bytes() == (
            tmp_path / "b" / "note.jsonl"
        ).read_bytes()

    def test_unknown_cache_mode_exits_two(self, mock_project: Path, tmp_path: Path) -> None:
        result = invoke("generate", str(mock_project), "-d", str(tmp_path), "--cache", "telepathy")
        assert result.exit_code == 2

    def test_llm_batch_size_reduces_calls(self, tmp_path: Path) -> None:
        path = tmp_path / "batch.yaml"
        path.write_text(
            self.MOCK_YAML.replace(
                "        generator: llm", "        generator: llm\n        mode: batch"
            ),
            encoding="utf-8",
        )
        result = invoke("generate", str(path), "-d", str(tmp_path / "out"), "--llm-batch-size", "6")
        assert result.exit_code == 0, result.stdout
        assert "language-model calls  1" in result.stdout

    def test_preview_uses_the_provider(self, mock_project: Path) -> None:
        result = invoke("preview", str(mock_project), "-n", "3", "--json")
        assert result.exit_code == 0
        rows = [json.loads(line) for line in result.stdout.strip().splitlines()]
        assert all(row["body"] for row in rows)


class TestRunCommands:
    """Run history, the inspector and resume (sections 32, 37, 56)."""

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        """A project file with its own store directory beside it."""
        path = tmp_path / "proj.yaml"
        path.write_text(SIMPLE_YAML, encoding="utf-8")
        return path

    def test_generate_records_a_run(self, workspace: Path, tmp_path: Path) -> None:
        result = invoke("generate", str(workspace), "-d", str(tmp_path / "out"))
        assert result.exit_code == 0, result.stdout
        assert "run " in result.stdout

        listing = invoke("runs", "--project", str(workspace), "--json")
        rows = json.loads(listing.stdout)
        assert len(rows) == 1
        assert rows[0]["state"] == "completed"
        assert rows[0]["records_written"] == 20

    def test_the_store_lands_beside_the_project(self, workspace: Path, tmp_path: Path) -> None:
        invoke("generate", str(workspace), "-d", str(tmp_path / "out"))
        assert (workspace.parent / ".cacophony" / "cacophony.db").exists()

    def test_history_can_be_switched_off(self, workspace: Path, tmp_path: Path) -> None:
        invoke("generate", str(workspace), "-d", str(tmp_path / "out"), "--no-history")
        assert not (workspace.parent / ".cacophony").exists()

    def test_a_custom_store_path(self, workspace: Path, tmp_path: Path) -> None:
        store = tmp_path / "elsewhere" / "runs.db"
        invoke("generate", str(workspace), "-d", str(tmp_path / "out"), "--store", str(store))
        assert store.exists()
        rows = json.loads(invoke("runs", "--store", str(store), "--json").stdout)
        assert len(rows) == 1

    def test_runs_without_a_store_says_so(self, tmp_path: Path) -> None:
        path = tmp_path / "unused.yaml"
        path.write_text(SIMPLE_YAML, encoding="utf-8")
        result = invoke("runs", "--project", str(path))
        assert result.exit_code == 0
        assert "no run store" in result.stdout

    def test_the_run_inspector(self, workspace: Path, tmp_path: Path) -> None:
        """Section 56: completed, duration, records, errors, output size."""
        invoke("generate", str(workspace), "-d", str(tmp_path / "out"))
        run_id = json.loads(invoke("runs", "--project", str(workspace), "--json").stdout)[0]["id"]

        result = invoke("run", run_id, "--project", str(workspace))
        assert result.exit_code == 0
        assert "completed" in result.stdout
        assert "widget" in result.stdout
        assert "quality" in result.stdout
        assert "constraint_validity" in result.stdout

    def test_the_inspector_accepts_a_prefix(self, workspace: Path, tmp_path: Path) -> None:
        invoke("generate", str(workspace), "-d", str(tmp_path / "out"))
        run_id = json.loads(invoke("runs", "--project", str(workspace), "--json").stdout)[0]["id"]
        assert invoke("run", run_id[:8], "--project", str(workspace)).exit_code == 0

    def test_an_unknown_run_exits_two(self, workspace: Path, tmp_path: Path) -> None:
        invoke("generate", str(workspace), "-d", str(tmp_path / "out"))
        result = invoke("run", "zzzzzzzz", "--project", str(workspace))
        assert result.exit_code == 2
        assert "no run matching" in result.stderr

    def test_the_inspector_as_json(self, workspace: Path, tmp_path: Path) -> None:
        invoke("generate", str(workspace), "-d", str(tmp_path / "out"))
        run_id = json.loads(invoke("runs", "--project", str(workspace), "--json").stdout)[0]["id"]
        payload = json.loads(invoke("run", run_id, "--project", str(workspace), "--json").stdout)
        assert payload["state"] == "completed"
        assert len(payload["jobs"]) == 1
        assert payload["summary"]["files"]

    def test_the_schema_revision_is_recorded(self, workspace: Path, tmp_path: Path) -> None:
        """Section 73: a run records the exact schema it used."""
        invoke("generate", str(workspace), "-d", str(tmp_path / "out"))
        run_id = json.loads(invoke("runs", "--project", str(workspace), "--json").stdout)[0]["id"]
        payload = json.loads(invoke("run", run_id, "--project", str(workspace), "--json").stdout)
        assert payload["revision_id"] is not None

    def test_resume_with_nothing_to_resume(self, workspace: Path, tmp_path: Path) -> None:
        invoke("generate", str(workspace), "-d", str(tmp_path / "out"))
        result = invoke("resume", "--project", str(workspace))
        assert result.exit_code == 2
        assert "no resumable run" in result.stderr

    def test_resume_without_a_store(self, tmp_path: Path) -> None:
        path = tmp_path / "unused.yaml"
        path.write_text(SIMPLE_YAML, encoding="utf-8")
        result = invoke("resume", "--project", str(path))
        assert result.exit_code == 2
        assert "no run store" in result.stderr

    def test_a_failed_run_exits_three_and_offers_a_resume(self, tmp_path: Path) -> None:
        path = tmp_path / "boom.yaml"
        path.write_text(
            """
project:
  name: Boom
  seed: 1
entities:
  e:
    count: 5
    fields:
      words:
        type: text
        semantic: Some prose
        generator: llm
""",
            encoding="utf-8",
        )
        result = invoke("generate", str(path), "-d", str(tmp_path / "out"))
        assert result.exit_code == 3
        assert "resume with" in result.stdout

        rows = json.loads(invoke("runs", "--project", str(path), "--json").stdout)
        assert rows[0]["state"] == "failed"

    def test_resume_uses_the_schema_the_run_started_with(self, tmp_path: Path) -> None:
        """Section 73. Resuming under an edited schema would produce a dataset
        generated two different ways, so the recorded revision wins."""
        path = tmp_path / "fixable.yaml"
        broken = """
project:
  name: Fixable
  seed: 1
entities:
  e:
    count: 6
    fields:
      id:
        type: string
        generator: sequence
      words:
        type: text
        semantic: Some prose
        generator: llm
"""
        path.write_text(broken, encoding="utf-8")
        assert invoke("generate", str(path), "-d", str(tmp_path / "out")).exit_code == 3

        fixed = broken.replace(
            "        generator: llm",
            "        generator: llm\n        on_unavailable: placeholder",
        )
        path.write_text(fixed, encoding="utf-8")

        # The resume continues under revision 1, so it fails the same way.
        result = invoke("resume", "--project", str(path))
        assert result.exit_code == 3
        assert "revision" in result.stdout

    def test_a_fresh_run_picks_up_a_fixed_schema(self, tmp_path: Path) -> None:
        path = tmp_path / "fixable.yaml"
        broken = """
project:
  name: Fixable
  seed: 1
entities:
  e:
    count: 6
    fields:
      id:
        type: string
        generator: sequence
      words:
        type: text
        semantic: Some prose
        generator: llm
"""
        path.write_text(broken, encoding="utf-8")
        assert invoke("generate", str(path), "-d", str(tmp_path / "out")).exit_code == 3

        path.write_text(
            broken.replace(
                "        generator: llm",
                "        generator: llm\n        on_unavailable: placeholder",
            ),
            encoding="utf-8",
        )
        assert invoke("generate", str(path), "-d", str(tmp_path / "out2")).exit_code == 0
        rows = (tmp_path / "out2" / "e.jsonl").read_text().strip().splitlines()
        assert len(rows) == 6

        # And the store now holds two revisions of the schema.
        payload = json.loads(invoke("runs", "--project", str(path), "--json").stdout)
        assert len({row["id"] for row in payload}) == 2

    def test_workers_and_checkpoint_options_are_accepted(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        result = invoke(
            "generate",
            str(workspace),
            "-d",
            str(tmp_path / "out"),
            "--workers",
            "2",
            "--checkpoint-every",
            "5",
            "--batch-size",
            "5",
        )
        assert result.exit_code == 0, result.stdout

    def test_json_logging(self, workspace: Path, tmp_path: Path) -> None:
        """Section 86: structured logs, one JSON object per line."""
        result = invoke(
            "generate",
            str(workspace),
            "-d",
            str(tmp_path / "out"),
            "--log-level",
            "info",
            "--log-format",
            "json",
        )
        assert result.exit_code == 0
        lines = [line for line in result.stderr.splitlines() if line.startswith("{")]
        assert lines
        payload = json.loads(lines[0])
        assert payload["run_id"]
        assert "timestamp" in payload and "level" in payload


class TestShippedTemplatesEndToEnd:
    def test_corporate_directory_generates(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke(
            "generate",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-n",
            "50",
            "-d",
            str(out),
            "-o",
            "jsonl",
        )
        assert result.exit_code == 0, result.stdout
        for entity in ("employee", "device", "location"):
            lines = (out / f"{entity}.jsonl").read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 50

    def test_generated_emails_use_reserved_domains(self, tmp_path: Path) -> None:
        """Section 62, verified on real output rather than on the schema."""
        out = tmp_path / "out"
        invoke(
            "generate",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-n",
            "100",
            "-e",
            "employee",
            "-d",
            str(out),
        )
        for line in (out / "employee.jsonl").read_text(encoding="utf-8").splitlines():
            domain = json.loads(line)["email"].split("@")[1]
            assert domain in {"example.com", "example.org", "example.net"} or domain.endswith(
                (".example", ".test", ".invalid")
            )

    def test_helpdesk_runs_with_placeholders(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke("generate", str(TEMPLATES / "helpdesk.yaml"), "-n", "20", "-d", str(out))
        assert result.exit_code == 0, result.stdout
        first = json.loads((out / "ticket.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert "PLACEHOLDER" in first["summary"]

    def test_security_operations_generates(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke(
            "generate", str(TEMPLATES / "security-operations.yaml"), "-n", "25", "-d", str(out)
        )
        assert result.exit_code == 0, result.stdout
        row = json.loads((out / "authentication.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert row["source_ip"].startswith(("192.0.2.", "198.51.100.", "203.0.113."))


# --------------------------------------------------------------------------- #
# Relational generation and the schema assistant (sections 15, 33, 50)
# --------------------------------------------------------------------------- #

RELATIONAL_YAML = """
project:
  name: CLI Relational
  seed: 8080

entities:
  supplier:
    count: 6
    primary_key: supplier_id
    fields:
      supplier_id:
        type: integer
        generator: sequence
      name:
        generator: faker
        provider: company

  part:
    count: 50
    primary_key: part_id
    fields:
      part_id:
        type: integer
        generator: sequence
      supplier:
        generator: reference
        entity: supplier
        distribution: skewed
      grade:
        type: enum
        generator: weighted
        choices:
          standard: 70
          premium: 30
"""

MOCK_PROVIDER_YAML = """
project:
  name: Assistant Host
  seed: 1
providers:
  designer:
    type: language_model
    adapter: mock
    model: mock-designer
entities:
  placeholder:
    count: 1
    fields:
      id:
        type: integer
        generator: sequence
"""


@pytest.fixture
def relational_file(tmp_path: Path) -> Path:
    path = tmp_path / "relational.yaml"
    path.write_text(RELATIONAL_YAML, encoding="utf-8")
    return path


class TestRelationalCli:
    def test_sqlite_output_is_one_database_with_a_working_join(
        self, relational_file: Path, tmp_path: Path
    ) -> None:
        import sqlite3

        out = tmp_path / "db"
        result = invoke("generate", str(relational_file), "-o", "sqlite", "-d", str(out))
        assert result.exit_code == 0, result.stdout

        files = list(out.glob("*.db"))
        assert [path.name for path in files] == ["cli-relational.db"]

        connection = sqlite3.connect(files[0])
        try:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            joined = connection.execute(
                "SELECT COUNT(*) FROM part p JOIN supplier s ON p.supplier = s.supplier_id"
            ).fetchone()[0]
            assert joined == 50
        finally:
            connection.close()

    def test_a_sql_script_loads_into_a_database(
        self, relational_file: Path, tmp_path: Path
    ) -> None:
        import sqlite3

        out = tmp_path / "sql"
        assert invoke("generate", str(relational_file), "-o", "sql", "-d", str(out)).exit_code == 0

        script = (out / "supplier.sql").read_text() + (out / "part.sql").read_text()
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(script)
            assert connection.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 50
        finally:
            connection.close()

    def test_the_run_reports_referential_integrity(
        self, relational_file: Path, tmp_path: Path
    ) -> None:
        result = invoke("generate", str(relational_file), "-d", str(tmp_path / "out"))
        assert result.exit_code == 0
        assert "referential" in result.stdout
        assert "distributions" in result.stdout

    def test_a_record_override_keeps_references_inside_the_run(
        self, relational_file: Path, tmp_path: Path
    ) -> None:
        """`--records 4` must not produce a reference to supplier 6."""
        import json

        out = tmp_path / "small"
        assert invoke("generate", str(relational_file), "-d", str(out), "-n", "4").exit_code == 0

        parts = [json.loads(line) for line in (out / "part.jsonl").read_text().splitlines()]
        assert all(1 <= part["supplier"] <= 4 for part in parts)

    def test_lint_reports_a_reference_problem(self, tmp_path: Path) -> None:
        broken = RELATIONAL_YAML.replace(
            "      supplier:\n        generator: reference\n        entity: supplier\n",
            "      supplier:\n        generator: reference\n        entity: supplier\n        unique: true\n",
        )
        path = tmp_path / "broken.yaml"
        path.write_text(broken, encoding="utf-8")

        result = invoke("lint", str(path))
        assert result.exit_code == 1
        assert "unique-reference-overflow" in result.stdout


class TestPropose:
    @pytest.fixture
    def provider_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "provider.yaml"
        path.write_text(MOCK_PROVIDER_YAML, encoding="utf-8")
        return path

    def test_it_writes_a_schema_that_validates(self, provider_file: Path, tmp_path: Path) -> None:
        out = tmp_path / "proposed.yaml"
        result = invoke(
            "propose", "a library of books", "--providers", str(provider_file), "--out", str(out)
        )
        assert result.exit_code == 0, result.stdout
        assert out.exists()
        # Whatever the model said, what was written has to compile.
        assert invoke("validate", str(out)).exit_code == 0

    def test_it_prints_the_schema_when_no_file_is_named(self, provider_file: Path) -> None:
        result = invoke("propose", "a library", "--providers", str(provider_file))
        assert result.exit_code == 0, result.stdout
        assert "project:" in result.stdout
        assert "entities:" in result.stdout

    def test_it_refuses_to_overwrite_without_force(
        self, provider_file: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "existing.yaml"
        out.write_text("keep me", encoding="utf-8")

        result = invoke(
            "propose", "a library", "--providers", str(provider_file), "--out", str(out)
        )
        assert result.exit_code == 2
        assert "--force" in result.stderr
        assert out.read_text() == "keep me"

    def test_force_overwrites(self, provider_file: Path, tmp_path: Path) -> None:
        out = tmp_path / "existing.yaml"
        out.write_text("keep me", encoding="utf-8")

        result = invoke(
            "propose",
            "a library",
            "--providers",
            str(provider_file),
            "--out",
            str(out),
            "--force",
        )
        assert result.exit_code == 0, result.stdout
        assert out.read_text() != "keep me"

    def test_a_project_with_no_language_model_is_refused(
        self, project_file: Path, tmp_path: Path
    ) -> None:
        result = invoke("propose", "a library", "--providers", str(project_file))
        assert result.exit_code == 2
        # Errors go to stderr, so a piped proposal stays machine-readable.
        assert "no language model" in result.stderr


class TestDistributedCommands:
    """``cluster``, ``controller`` and ``worker`` (sections 84, 95)."""

    def test_cluster_generates_and_joins(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke(
            "cluster",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-o",
            str(out),
            "-w",
            "3",
            "--shard-size",
            "37",
            "-n",
            "150",
        )
        assert result.exit_code == 0, result.stdout
        for entity in ("employee", "device", "location"):
            lines = (out / f"{entity}.jsonl").read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 150
        # The parts are gone once they have been joined.
        assert not list(out.glob("*.part*.jsonl"))

    def test_cluster_output_matches_generate(self, tmp_path: Path) -> None:
        """The claim, through the two commands a user would actually compare."""
        single, many = tmp_path / "single", tmp_path / "many"
        template = str(TEMPLATES / "corporate-directory.yaml")

        assert invoke("generate", template, "-n", "120", "-d", str(single)).exit_code == 0
        assert (
            invoke(
                "cluster", template, "-n", "120", "-o", str(many), "-w", "4", "--shard-size", "29"
            )
        ).exit_code == 0

        for entity in ("employee", "device", "location"):
            assert (single / f"{entity}.jsonl").read_bytes() == (
                many / f"{entity}.jsonl"
            ).read_bytes(), entity

    def test_cluster_keeps_parts_when_asked(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke(
            "cluster",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-o",
            str(out),
            "-f",
            "parquet",
            "--no-join",
            "-n",
            "60",
            "--shard-size",
            "25",
        )
        assert result.exit_code == 0, result.stdout
        assert len(list(out.glob("employee.part*.parquet"))) == 3

    def test_parquet_cannot_be_joined(self, tmp_path: Path) -> None:
        result = invoke(
            "cluster",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-o",
            str(tmp_path / "out"),
            "-f",
            "parquet",
            "-n",
            "10",
        )
        assert result.exit_code == 2
        assert "per-file footer" in result.stderr

    def test_a_database_cannot_be_sharded(self, tmp_path: Path) -> None:
        result = invoke(
            "cluster",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-o",
            str(tmp_path / "out"),
            "-f",
            "sqlite",
            "-n",
            "10",
        )
        assert result.exit_code == 2
        assert "foreign keys would not resolve" in result.stderr

    def test_a_worker_needs_a_reachable_controller(self, tmp_path: Path) -> None:
        result = invoke(
            "worker",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-c",
            "http://127.0.0.1:1",  # nothing listens here
            "-o",
            str(tmp_path / "out"),
        )
        assert result.exit_code == 1
        assert "unreachable" in result.stderr

    def test_an_unknown_capability_is_refused(self, tmp_path: Path) -> None:
        result = invoke(
            "worker",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-c",
            "http://127.0.0.1:1",
            "-o",
            str(tmp_path / "out"),
            "--capabilities",
            "deterministic,telepathy",
        )
        assert result.exit_code == 2
        assert "telepathy" in result.stderr


class TestOutputProfiles:
    """Section 34's ``outputs:`` block, which was parsed and ignored for a while."""

    PROJECT = """
project:
  name: Profiles
  seed: 4
entities:
  reading:
    count: 30
    tags: [telemetry]
    fields:
      id: {type: integer, generator: sequence}
      site:
        type: enum
        generator: weighted
        choices: {north: 1, south: 1}
outputs:
  analytics:
    format: jsonl
    path: out/analytics
    partition_by: [site]
  fixtures:
    format: csv
    path: out/fixtures
"""

    def _project(self, tmp_path: Path) -> Path:
        path = tmp_path / "profiles.yaml"
        path.write_text(self.PROJECT, encoding="utf-8")
        return path

    def test_a_profile_chooses_the_format_and_the_path(self, tmp_path: Path) -> None:
        """And its relative path resolves against the schema, as every path does."""
        project = self._project(tmp_path)
        result = invoke("generate", str(project), "--output-profile", "fixtures")
        assert result.exit_code == 0, result.stdout
        assert (tmp_path / "out" / "fixtures" / "reading.csv").is_file()

    def test_partition_by_builds_the_directories(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        result = invoke("generate", str(project), "--output-profile", "analytics")
        assert result.exit_code == 0, result.stdout
        root = tmp_path / "out" / "analytics" / "reading"
        assert sorted(child.name for child in root.iterdir()) == ["site=north", "site=south"]

    def test_an_explicit_flag_beats_the_profile(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        result = invoke(
            "generate", str(project), "--output-profile", "fixtures", "-d", str(elsewhere)
        )
        assert result.exit_code == 0, result.stdout
        assert (elsewhere / "reading.csv").is_file()

    def test_an_unknown_profile_names_the_ones_there_are(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        result = invoke("generate", str(project), "--output-profile", "nope")
        assert result.exit_code == 2
        assert "analytics, fixtures" in result.stderr

    def test_without_a_profile_nothing_changes(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        result = invoke("generate", str(project), "-d", str(tmp_path / "plain"))
        assert result.exit_code == 0, result.stdout
        assert (tmp_path / "plain" / "reading.jsonl").is_file()

    def test_tags_reach_the_plan(self, tmp_path: Path) -> None:
        """They were accepted and dropped; the manual claimed otherwise."""
        result = invoke("plan", str(self._project(tmp_path)), "--json")
        assert result.exit_code == 0, result.stdout
        assert json.loads(result.stdout)["steps"][0]["tags"] == ["telemetry"]


class TestRecordCounts:
    PROJECT = """
project: {name: Counts, seed: 9}
entities:
  parent:
    count: 6
    primary_key: parent_id
    fields:
      parent_id: {type: integer, generator: sequence}
  child:
    count: 50
    fields:
      child_id: {type: integer, generator: sequence}
      parent: {generator: reference, entity: parent}
"""

    def _project(self, tmp_path: Path) -> Path:
        path = tmp_path / "counts.yaml"
        path.write_text(self.PROJECT, encoding="utf-8")
        return path

    def _lines(self, path: Path) -> int:
        return len(path.read_text(encoding="utf-8").strip().splitlines())

    def test_a_named_count_leaves_the_others_alone(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke("generate", str(self._project(tmp_path)), "-n", "child=12", "-d", str(out))
        assert result.exit_code == 0, result.stdout
        assert self._lines(out / "child.jsonl") == 12
        assert self._lines(out / "parent.jsonl") == 6

    def test_a_bare_count_still_overrides_everything(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke("generate", str(self._project(tmp_path)), "-n", "4", "-d", str(out))
        assert result.exit_code == 0, result.stdout
        assert self._lines(out / "child.jsonl") == 4
        assert self._lines(out / "parent.jsonl") == 4

    def test_a_named_count_beats_the_bare_one(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke(
            "generate", str(self._project(tmp_path)), "-n", "4", "-n", "child=9", "-d", str(out)
        )
        assert result.exit_code == 0, result.stdout
        assert self._lines(out / "child.jsonl") == 9
        assert self._lines(out / "parent.jsonl") == 4

    def test_an_unknown_entity_is_refused(self, tmp_path: Path) -> None:
        result = invoke("generate", str(self._project(tmp_path)), "-n", "ghost=5")
        assert result.exit_code == 2
        assert "no entity 'ghost'" in result.stderr

    def test_something_that_is_not_a_number_is_refused(self, tmp_path: Path) -> None:
        result = invoke("generate", str(self._project(tmp_path)), "-n", "lots")
        assert result.exit_code == 2
        assert "ENTITY=NUMBER" in result.stderr


# --------------------------------------------------------------------------- #
# begin (design document section 110)
# --------------------------------------------------------------------------- #

#: A mock provider with an exact answer, so the flow can be tested rather than
#: the model. Without it the mock invents a schema afresh every call, which is
#: the right behaviour for a stand-in and useless for an assertion.
SCRIPTED_PROPOSAL = json.dumps(
    {
        "name": "Small Hospital",
        "description": "Staff and the wards they work on.",
        "entities": [
            {
                "name": "ward",
                "count": 8,
                "fields": [
                    {"name": "ward_id", "type": "integer", "semantic": "ward number"},
                    {"name": "ward_name", "type": "string", "semantic": "the name of a ward"},
                ],
            },
            {
                "name": "nurse",
                "count": 40,
                "fields": [
                    {"name": "nurse_id", "type": "integer", "semantic": "staff number"},
                    {"name": "full_name", "type": "string", "semantic": "a person's full name"},
                    {"name": "ward", "type": "integer", "semantic": "ward", "references": "ward"},
                ],
            },
        ],
    }
)

SCRIPTED_PROVIDER_YAML = f"""
project:
  name: Scripted Assistant
  seed: 1
providers:
  designer:
    type: language_model
    adapter: mock
    model: mock-designer
    options:
      responses:
        # A block scalar, so the apostrophe in "a person's full name" does not
        # end a quoted string halfway through the JSON.
        - |-
          {SCRIPTED_PROPOSAL}
entities:
  placeholder:
    count: 1
    fields:
      id:
        type: integer
        generator: sequence
"""


class TestBegin:
    """One sentence to a world: propose, review, generate."""

    @pytest.fixture
    def scripted(self, tmp_path: Path) -> Path:
        path = tmp_path / "assistant.yaml"
        path.write_text(SCRIPTED_PROVIDER_YAML, encoding="utf-8")
        return path

    def test_it_proposes_writes_and_generates_in_one_go(
        self, scripted: Path, tmp_path: Path
    ) -> None:
        schema = tmp_path / "hospital.yaml"
        out = tmp_path / "world"
        result = invoke(
            "begin",
            "a small hospital",
            "--providers",
            str(scripted),
            "--out",
            str(schema),
            "-d",
            str(out),
            "--yes",
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # The schema, because a world nobody can regenerate is an anecdote.
        assert schema.exists()
        assert invoke("validate", str(schema)).exit_code == 0
        # And the data.
        assert len((out / "ward.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 8
        assert len((out / "nurse.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 40

    def test_the_references_it_proposed_resolve(self, scripted: Path, tmp_path: Path) -> None:
        """The point of proposing a relationship is that it is a real one.

        This proposal declares no primary key anywhere - the model is not
        required to, and this one did not. Cacophony supplies one for the
        entity being referenced, because otherwise the reference points at
        nothing and the first join fails.
        """
        schema = tmp_path / "hospital.yaml"
        out = tmp_path / "world"
        invoke(
            "begin",
            "a small hospital",
            "--providers",
            str(scripted),
            "--out",
            str(schema),
            "-d",
            str(out),
            "--yes",
        )
        wards = {
            json.loads(line)["ward_id"]
            for line in (out / "ward.jsonl").read_text(encoding="utf-8").splitlines()
        }
        nurses = [
            json.loads(line)
            for line in (out / "nurse.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert nurses and all(nurse["ward"] in wards for nurse in nurses)

    def test_it_says_what_it_is_about_to_build(self, scripted: Path, tmp_path: Path) -> None:
        result = invoke(
            "begin",
            "a small hospital",
            "--providers",
            str(scripted),
            "--out",
            str(tmp_path / "h.yaml"),
            "-d",
            str(tmp_path / "w"),
            "--yes",
        )
        assert "proposed 2 entities, 48 records" in result.stdout
        assert "BEGIN CACOPHONY" in result.stdout

    def test_scale_shrinks_the_world(self, scripted: Path, tmp_path: Path) -> None:
        out = tmp_path / "world"
        result = invoke(
            "begin",
            "a small hospital",
            "--providers",
            str(scripted),
            "--out",
            str(tmp_path / "h.yaml"),
            "-d",
            str(out),
            "--yes",
            "--scale",
            "8",
        )
        assert result.exit_code == 0, result.stdout
        assert len((out / "nurse.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 5

    def test_it_will_not_overwrite_a_schema(self, scripted: Path, tmp_path: Path) -> None:
        schema = tmp_path / "taken.yaml"
        schema.write_text("project:\n  name: mine\n", encoding="utf-8")
        result = invoke(
            "begin",
            "a small hospital",
            "--providers",
            str(scripted),
            "--out",
            str(schema),
            "--yes",
        )
        assert result.exit_code == 2
        assert "--force" in result.stderr
        assert schema.read_text(encoding="utf-8") == "project:\n  name: mine\n"

    def test_the_output_format_is_the_one_asked_for(self, scripted: Path, tmp_path: Path) -> None:
        out = tmp_path / "world"
        result = invoke(
            "begin",
            "a small hospital",
            "--providers",
            str(scripted),
            "--out",
            str(tmp_path / "h.yaml"),
            "-d",
            str(out),
            "--yes",
            "-f",
            "csv",
        )
        assert result.exit_code == 0, result.stdout
        assert (out / "nurse.csv").is_file()

    def test_the_run_is_recorded_against_the_schema_it_wrote(
        self, scripted: Path, tmp_path: Path
    ) -> None:
        """So `cacophony runs` finds it, and `generate` reproduces it."""
        schema = tmp_path / "hospital.yaml"
        invoke(
            "begin",
            "a small hospital",
            "--providers",
            str(scripted),
            "--out",
            str(schema),
            "-d",
            str(tmp_path / "w"),
            "--yes",
        )
        listed = invoke("runs", "--project", str(schema), "--json")
        assert listed.exit_code == 0, listed.stdout
        assert json.loads(listed.stdout)
