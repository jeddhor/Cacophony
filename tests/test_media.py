"""Multimodal generation (design document sections 18-23, 81, 82).

Three claims are worth checking rather than assuming.

The *formats are real*. A PNG that only Cacophony can read, or a PDF no reader
accepts, would be worse than no image generation at all - it would look like it
worked. So the encoders are checked against their actual signatures and
structures rather than against themselves.

The *pipeline is a pipeline*. Section 82 wants one record to produce several
artifacts, each derived from that record's own values. A badge carrying
somebody else's name would pass any test that only counted files.

Nothing is *paid for twice*. The reuse path is the difference between a resumed
portrait run taking seconds and taking hours.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from cacophony.assets.audio import (
    concatenate,
    duration_of,
    encode_wav,
    estimate_seconds,
    is_wav,
    silence,
    speech_like,
)
from cacophony.assets.documents import PAGE_SIZES, Document, render_template
from cacophony.assets.imaging import Canvas, encode_png, is_png
from cacophony.assets.store import AssetStore, extension_for
from cacophony.core.errors import GenerationError
from cacophony.generation.engine import GenerationEngine
from cacophony.generation.runtime import GenerationRuntime
from cacophony.providers.base import ImageRequest, SpeechRequest
from cacophony.providers.image.procedural import ProceduralImageProvider
from cacophony.providers.speech.procedural import ProceduralSpeechProvider
from helpers import TEMPLATES, compile_from, make_project

MULTIMODAL = TEMPLATES / "multimodal-support.yaml"


# --------------------------------------------------------------------------- #
# Encoders
# --------------------------------------------------------------------------- #


class TestImaging:
    def test_a_png_is_a_png(self) -> None:
        data = encode_png(4, 3, bytes([255, 0, 0]) * 12)
        assert is_png(data)
        # IHDR carries the dimensions, big-endian, right after the length+tag.
        assert data[16:24] == (4).to_bytes(4, "big") + (3).to_bytes(4, "big")

    def test_pixel_data_must_match_the_dimensions(self) -> None:
        with pytest.raises(ValueError, match="expected"):
            encode_png(4, 4, b"\x00\x00\x00")

    def test_canvas_drawing_lands_where_it_is_told(self) -> None:
        canvas = Canvas(4, 4, (0, 0, 0))
        canvas.rectangle(1, 1, 2, 2, (255, 255, 255))
        assert canvas.pixels[0:3] == bytes([0, 0, 0])
        assert canvas.pixels[(1 * 4 + 1) * 3 : (1 * 4 + 1) * 3 + 3] == bytes([255, 255, 255])

    def test_drawing_outside_the_canvas_is_clipped_not_fatal(self) -> None:
        canvas = Canvas(4, 4)
        canvas.rectangle(-10, -10, 2, 2, (1, 2, 3))
        canvas.rectangle(100, 100, 5, 5, (1, 2, 3))
        assert len(canvas.pixels) == 4 * 4 * 3

    def test_a_gradient_actually_changes_down_the_image(self) -> None:
        canvas = Canvas(2, 8)
        canvas.vertical_gradient((0, 0, 0), (255, 255, 255))
        top = canvas.pixels[0]
        bottom = canvas.pixels[(7 * 2) * 3]
        assert bottom > top


class TestAudio:
    def test_a_wav_is_a_wav(self) -> None:
        data = encode_wav([0.0, 0.5, -0.5], sample_rate=8000)
        assert is_wav(data)
        assert duration_of(data) == pytest.approx(3 / 8000)

    def test_synthesis_length_tracks_the_text(self) -> None:
        short = duration_of(speech_like("Hello.", seed=1))
        long = duration_of(speech_like("Hello. " * 20, seed=1))
        assert long > short * 5

    def test_it_is_deterministic(self) -> None:
        assert speech_like("same words", seed=7) == speech_like("same words", seed=7)

    def test_different_voices_sound_different(self) -> None:
        assert speech_like("hello", seed=100) != speech_like("hello", seed=220)

    def test_samples_stay_in_range(self) -> None:
        """A sample outside [-1, 1] clips into a click when it is packed."""
        data = encode_wav([5.0, -5.0])
        assert is_wav(data)

    def test_silence_has_a_duration(self) -> None:
        assert duration_of(silence(0.5)) == pytest.approx(0.5, abs=0.01)

    def test_clips_join_with_a_gap(self) -> None:
        first = speech_like("one", seed=1)
        second = speech_like("two", seed=2)
        joined = concatenate([first, second], gap_seconds=0.5)
        assert duration_of(joined) == pytest.approx(
            duration_of(first) + duration_of(second) + 0.5, abs=0.02
        )

    def test_mismatched_rates_are_refused_rather_than_resampled(self) -> None:
        with pytest.raises(ValueError, match="Hz"):
            concatenate([encode_wav([0.0], sample_rate=8000), encode_wav([0.0], sample_rate=16000)])

    def test_estimated_speed_scales_the_duration(self) -> None:
        assert estimate_seconds("a sentence", speed=2.0) < estimate_seconds("a sentence")


class TestDocuments:
    def test_placeholders_are_filled(self) -> None:
        assert render_template("{a} and {b}", {"a": "x", "b": "y"}) == "x and y"

    def test_dotted_names_read_a_related_record(self) -> None:
        assert render_template("{c.name}", {"c": {"name": "Aurora"}}) == "Aurora"

    def test_a_missing_field_is_empty_by_default(self) -> None:
        assert render_template("[{nope}]", {}) == "[]"

    def test_a_missing_field_can_be_an_error(self) -> None:
        with pytest.raises(KeyError, match="nope"):
            render_template("{nope}", {}, on_missing="error")

    def test_a_field_named_after_its_entity_keeps_its_own_value(self) -> None:
        """`{agent}` is the key it holds; `{agent.first_name}` is the record.

        Merging the two rendered a whole Python dict into the document, which
        is what a real run put on a transcript.
        """
        values = {"agent": "SUP-0001"}
        related = {"agent": {"first_name": "Denise", "employee_id": "SUP-0001"}}
        assert render_template("{agent}", values, related=related) == "SUP-0001"
        assert render_template("{agent.first_name}", values, related=related) == "Denise"

    def test_dates_are_rendered_iso_not_repr(self) -> None:
        """A badge reading `datetime.date(2024, 12, 5)` is a bug the reader sees."""
        import datetime as dt

        rendered = render_template(
            "{joined} {at}",
            {"joined": dt.date(2024, 12, 5), "at": dt.datetime(2026, 5, 23, 8, 38)},
        )
        assert rendered == "2024-12-05 2026-05-23 08:38:00"

    def test_a_pdf_is_a_pdf(self) -> None:
        pdf = Document(title="T").layout("hello").to_pdf()
        assert pdf.startswith(b"%PDF-1.4")
        assert pdf.rstrip().endswith(b"%%EOF")
        assert b"/Type /Catalog" in pdf
        assert b"startxref" in pdf

    def test_the_xref_offsets_point_at_real_objects(self) -> None:
        """A wrong offset is the classic way to produce a PDF nothing opens."""
        pdf = Document().layout("a line").to_pdf()
        start = pdf.index(b"xref\n")
        lines = pdf[start:].split(b"\n")
        # Skip "xref" and the "0 N" subsection header, then the free entry.
        for number, row in enumerate(lines[3:], start=1):
            if not row.endswith(b" n "):
                break
            offset = int(row.split()[0])
            assert pdf[offset:].startswith(f"{number} 0 obj".encode())

    def test_long_text_paginates(self) -> None:
        document = Document(page_size="a5").layout("word " * 4000)
        assert len(document.pages) > 1

    def test_parentheses_in_a_value_do_not_break_the_syntax(self) -> None:
        pdf = Document().layout("a (b) c \\ d").to_pdf()
        assert rb"\(b\)" in pdf

    def test_every_page_size_is_usable(self) -> None:
        for name in PAGE_SIZES:
            assert Document(page_size=name).layout("x").to_pdf().startswith(b"%PDF")

    def test_an_unknown_page_size_falls_back_rather_than_failing(self) -> None:
        assert Document(page_size="tabloid").dimensions == PAGE_SIZES["a4"]


# --------------------------------------------------------------------------- #
# The asset store
# --------------------------------------------------------------------------- #


class TestAssetStore:
    def _write(self, store: AssetStore, index: int, data: bytes = b"payload") -> Any:
        return store.write(
            data,
            entity="employee",
            record_index=index,
            field_name="portrait",
            kind="image",
            media_type="image/png",
            record_id=f"E{index}",
        )

    def test_paths_are_derived_from_position(self, tmp_path: Path) -> None:
        store = AssetStore(tmp_path)
        first = store.path_for("employee", 7, "portrait", media_type="image/png")
        again = store.path_for("employee", 7, "portrait", media_type="image/png")
        assert first == again
        assert first.name == "employee_00000007_portrait.png"

    def test_records_are_foldered_so_directories_stay_listable(self, tmp_path: Path) -> None:
        store = AssetStore(tmp_path)
        assert store.path_for("e", 999, "f").parent.name == "00000000"
        assert store.path_for("e", 1000, "f").parent.name == "00001000"

    def test_identical_bytes_are_stored_once(self, tmp_path: Path) -> None:
        store = AssetStore(tmp_path)
        for index in range(5):
            self._write(store, index)
        assert store.stats.written == 1
        assert store.stats.deduplicated == 4
        # Every logical asset still exists as a file at its own path.
        assert all(
            store.path_for("employee", index, "portrait", media_type="image/png").exists()
            for index in range(5)
        )

    def test_deduplication_can_be_switched_off(self, tmp_path: Path) -> None:
        store = AssetStore(tmp_path, deduplicate=False)
        for index in range(3):
            self._write(store, index)
        assert store.stats.written == 3

    def test_an_existing_file_is_not_rewritten(self, tmp_path: Path) -> None:
        store = AssetStore(tmp_path)
        self._write(store, 0, b"first")
        self._write(store, 0, b"second")
        assert store.path_for("employee", 0, "portrait", media_type="image/png").read_bytes() == (
            b"first"
        )
        assert store.stats.reused == 1

    def test_overwrite_replaces_it(self, tmp_path: Path) -> None:
        store = AssetStore(tmp_path, overwrite=True)
        self._write(store, 0, b"first")
        self._write(store, 0, b"second")
        assert store.path_for("employee", 0, "portrait", media_type="image/png").read_bytes() == (
            b"second"
        )

    def test_the_manifest_answers_what_belongs_to_a_record(self, tmp_path: Path) -> None:
        """Section 81's question."""
        store = AssetStore(tmp_path)
        self._write(store, 3, b"a")
        store.write(
            b"b",
            entity="employee",
            record_index=3,
            field_name="badge",
            kind="document",
            media_type="application/pdf",
        )
        store.close()

        belongs = store.assets_of("employee", 3)
        assert {row["field"] for row in belongs} == {"portrait", "badge"}
        assert {row["kind"] for row in belongs} == {"image", "document"}

    def test_the_manifest_is_one_json_object_per_line(self, tmp_path: Path) -> None:
        store = AssetStore(tmp_path)
        self._write(store, 0)
        store.close()
        lines = (tmp_path / "manifest.jsonl").read_text().strip().split("\n")
        assert all(json.loads(line)["entity"] == "employee" for line in lines)

    def test_reuse_is_counted_so_the_saving_is_visible(self, tmp_path: Path) -> None:
        from cacophony.assets.store import StoredAsset

        store = AssetStore(tmp_path)
        store.note_reuse(
            StoredAsset(
                entity="e",
                record_index=0,
                field="f",
                kind="image",
                path=tmp_path / "x.png",
                media_type="image/png",
                size_bytes=10,
                digest="",
            )
        )
        assert store.stats.reused == 1
        assert store.describe()["assets"] == 1

    @pytest.mark.parametrize(
        ("media_type", "suffix"),
        [
            ("image/png", ".png"),
            ("audio/wav", ".wav"),
            ("application/pdf", ".pdf"),
            ("text/plain", ".txt"),
            ("image/png; charset=binary", ".png"),
            ("application/x-unheard-of", ".bin"),
            (None, ".bin"),
        ],
    )
    def test_extensions(self, media_type: str | None, suffix: str) -> None:
        assert extension_for(media_type) == suffix


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


class TestProceduralProviders:
    def test_an_image_is_deterministic_for_a_seed(self) -> None:
        provider = ProceduralImageProvider("pictures")
        first = asyncio.run(provider.generate(ImageRequest(prompt="a face", seed=42)))
        second = asyncio.run(provider.generate(ImageRequest(prompt="a face", seed=42)))
        assert first.data == second.data
        assert is_png(first.data or b"")

    def test_a_different_seed_gives_a_different_image(self) -> None:
        provider = ProceduralImageProvider("pictures")
        first = asyncio.run(provider.generate(ImageRequest(prompt="a face", seed=1)))
        second = asyncio.run(provider.generate(ImageRequest(prompt="a face", seed=2)))
        assert first.data != second.data

    @pytest.mark.parametrize("style", ["identicon", "portrait", "card", "document"])
    def test_every_style_produces_a_valid_png(self, style: str) -> None:
        provider = ProceduralImageProvider("pictures", {"style": style})
        result = asyncio.run(
            provider.generate(ImageRequest(prompt="x", seed=5, width=64, height=64))
        )
        assert is_png(result.data or b"")
        assert result.width == 64

    def test_it_records_section_19s_provenance(self) -> None:
        provider = ProceduralImageProvider("pictures")
        result = asyncio.run(provider.generate(ImageRequest(prompt="a face", seed=9)))
        assert result.provider == "pictures"
        assert result.workflow
        assert result.seed == 9
        assert result.prompt_hash

    def test_it_never_claims_to_be_real(self) -> None:
        result = asyncio.run(ProceduralImageProvider("p").generate(ImageRequest(prompt="x")))
        assert result.raw["synthetic"] is True

    def test_speech_is_deterministic_and_labelled(self) -> None:
        provider = ProceduralSpeechProvider("voices")
        first = asyncio.run(provider.synthesize(SpeechRequest(text="hello", voice="agent")))
        second = asyncio.run(provider.synthesize(SpeechRequest(text="hello", voice="agent")))
        assert first.data == second.data
        assert first.raw["synthetic"] is True
        assert first.raw["spoken_text"] == "hello"

    def test_named_voices_differ(self) -> None:
        provider = ProceduralSpeechProvider("voices")
        agent = asyncio.run(provider.synthesize(SpeechRequest(text="hi", voice="agent")))
        customer = asyncio.run(provider.synthesize(SpeechRequest(text="hi", voice="customer")))
        assert agent.data != customer.data

    def test_an_unknown_voice_is_stable_rather_than_refused(self) -> None:
        provider = ProceduralSpeechProvider("voices")
        first = asyncio.run(provider.synthesize(SpeechRequest(text="hi", voice="zebra")))
        second = asyncio.run(provider.synthesize(SpeechRequest(text="hi", voice="zebra")))
        assert first.data == second.data

    def test_empty_text_gives_silence_rather_than_an_error(self) -> None:
        result = asyncio.run(ProceduralSpeechProvider("v").synthesize(SpeechRequest(text="  ")))
        assert is_wav(result.data or b"")

    def test_the_duration_is_reported(self) -> None:
        result = asyncio.run(
            ProceduralSpeechProvider("v").synthesize(SpeechRequest(text="a longer sentence here"))
        )
        assert (result.duration_seconds or 0) > 0.5

    def test_health_checks_say_what_they_are(self) -> None:
        assert asyncio.run(ProceduralImageProvider("p").health_check()).healthy
        assert asyncio.run(ProceduralSpeechProvider("s").health_check()).healthy


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


def media_project(**overrides: Any) -> Any:
    entities = {
        "employee": {
            "count": 3,
            "primary_key": "employee_id",
            "fields": {
                "employee_id": {"type": "string", "generator": "sequence", "format": "E-{000}"},
                "name": {"generator": "faker", "provider": "first_name"},
                "portrait": {
                    "type": "image",
                    "generator": "image",
                    "prompt": "headshot of {name}",
                    "width": 32,
                    "height": 32,
                    **overrides,
                },
                "badge": {
                    "type": "file",
                    "generator": "document",
                    "format": "txt",
                    "template": "{employee_id}\n{name}",
                    **overrides,
                },
            },
        }
    }
    return make_project(
        entities,
        providers={"pictures": {"type": "image", "adapter": "procedural_image"}},
    )


def run_media(tmp_path: Path, **overrides: Any) -> tuple[list[Any], AssetStore]:
    from cacophony.schema.compiler import compile_project

    spec = media_project(**overrides)
    compiled = compile_project(spec)
    store = AssetStore(tmp_path / "assets")
    store.open()
    engine = GenerationEngine(compiled, runtime=GenerationRuntime.for_project(spec), assets=store)
    records = asyncio.run(engine.generate_batch("employee", 3))
    store.close()
    return records, store


class TestMediaGeneration:
    def test_a_record_grows_assets(self, tmp_path: Path) -> None:
        records, _store = run_media(tmp_path)
        for record in records:
            kinds = {asset.kind for asset in record.assets}
            assert kinds == {"image", "document"}

    def test_the_field_value_is_the_path(self, tmp_path: Path) -> None:
        records, _store = run_media(tmp_path)
        assert records[0].values["portrait"].endswith(".png")
        assert "assets/employee" in records[0].values["portrait"].replace("\\", "/")

    def test_the_files_exist_and_are_the_right_format(self, tmp_path: Path) -> None:
        records, _store = run_media(tmp_path)
        for record in records:
            for asset in record.assets:
                assert asset.path.exists()
                data = asset.path.read_bytes()
                if asset.kind == "image":
                    assert is_png(data)

    def test_a_document_carries_this_record_s_own_values(self, tmp_path: Path) -> None:
        """Section 82: the artifact is derived from the record it belongs to."""
        records, _store = run_media(tmp_path)
        for record in records:
            badge = next(asset for asset in record.assets if asset.kind == "document")
            text = badge.path.read_text()
            assert record.values["employee_id"] in text
            assert record.values["name"] in text

    def test_the_second_run_reuses_rather_than_regenerating(self, tmp_path: Path) -> None:
        run_media(tmp_path)
        _records, store = run_media(tmp_path)
        assert store.stats.written == 0
        assert store.stats.reused > 0

    def test_reuse_can_be_switched_off_per_field(self, tmp_path: Path) -> None:
        run_media(tmp_path)
        _records, store = run_media(tmp_path, reuse=False)
        assert store.stats.reused < 6

    def test_the_prompt_depends_on_the_fields_it_names(self, tmp_path: Path) -> None:
        from cacophony.schema.compiler import compile_project

        compiled = compile_project(media_project())
        portrait = next(
            field for field in compiled.entity("employee").fields if field.name == "portrait"
        )
        assert "name" in portrait.dependencies

    def test_without_an_asset_store_the_policy_decides(self) -> None:
        from cacophony.schema.compiler import compile_project

        spec = media_project(on_unavailable="placeholder")
        compiled = compile_project(spec)
        engine = GenerationEngine(compiled, runtime=GenerationRuntime.for_project(spec))
        records = asyncio.run(engine.generate_batch("employee", 2))
        assert all("placeholder" in record.values["portrait"] for record in records)

    def test_a_missing_provider_is_explained(self) -> None:
        compiled = compile_from(
            {
                "e": {
                    "count": 1,
                    "fields": {"p": {"type": "image", "generator": "image", "prompt": "x"}},
                }
            }
        )
        engine = GenerationEngine(compiled)
        with pytest.raises(GenerationError):
            asyncio.run(engine.generate_batch("e", 1))

    def test_tts_requires_a_source(self) -> None:
        from cacophony.core.errors import GeneratorConfigError

        with pytest.raises(GeneratorConfigError, match="source"):
            compile_from(
                {"e": {"count": 1, "fields": {"a": {"type": "audio", "generator": "tts"}}}}
            )

    def test_a_document_requires_a_template(self) -> None:
        from cacophony.core.errors import GeneratorConfigError

        with pytest.raises(GeneratorConfigError, match="template"):
            compile_from({"e": {"count": 1, "fields": {"d": {"generator": "document"}}}})


# --------------------------------------------------------------------------- #
# The shipped template (section 82)
# --------------------------------------------------------------------------- #


class TestMultimodalTemplate:
    def test_it_compiles(self) -> None:
        from cacophony.schema.compiler import compile_project
        from cacophony.schema.loader import load_project

        compiled = compile_project(load_project(MULTIMODAL))
        assert compiled.entity_order == ("employee", "support_call")

    def test_a_reference_field_can_be_read_by_its_own_name(self) -> None:
        """`{agent.first_name}` where `agent` is the field, not the entity."""
        from cacophony.schema.compiler import compile_project
        from cacophony.schema.loader import load_project

        compiled = compile_project(load_project(MULTIMODAL))
        transcript = next(
            field for field in compiled.entity("support_call").fields if field.name == "transcript"
        )
        assert transcript.related_aliases == {"agent": "employee"}
        assert "employee" in transcript.related_entities

    def test_one_record_produces_several_artifacts(self, tmp_path: Path) -> None:
        from cacophony.runs.config import RunConfig
        from cacophony.runs.coordinator import Conductor
        from cacophony.schema.compiler import compile_project
        from cacophony.schema.loader import load_project

        compiled = compile_project(load_project(MULTIMODAL))
        config = RunConfig(output_dir=tmp_path, records=3, record_history=False)
        conductor = Conductor(compiled, config)
        outcome = asyncio.run(conductor.execute())
        asyncio.run(conductor.aclose())

        assert outcome.ok
        assets = outcome.summary.get("assets")
        assert assets and assets["assets"] == 12  # 3 x (portrait, badge) + 3 x (audio, txt)

        store = AssetStore(config.asset_root)
        first = store.assets_of("employee", 0)
        assert {row["field"] for row in first} == {"portrait", "id_badge"}

    def test_a_document_reads_the_reference_by_name_without_dumping_it(
        self, tmp_path: Path
    ) -> None:
        """The transcript names the agent's key, not a serialised record."""
        from cacophony.runs.config import RunConfig
        from cacophony.runs.coordinator import Conductor
        from cacophony.schema.compiler import compile_project
        from cacophony.schema.loader import load_project

        compiled = compile_project(load_project(MULTIMODAL))
        config = RunConfig(output_dir=tmp_path, records=2, record_history=False)
        conductor = Conductor(compiled, config)
        asyncio.run(conductor.execute())
        asyncio.run(conductor.aclose())

        store = AssetStore(config.asset_root)
        transcripts = [row for row in store.manifest() if row["path"].endswith(".txt")]
        assert transcripts
        for row in transcripts:
            text = Path(row["path"]).read_text()
            assert "{" not in text and "employee_id" not in text
            assert "SUP-" in text

    def test_the_audio_carries_its_transcript(self, tmp_path: Path) -> None:
        """Section 21: an aligned transcript is what makes a speech set usable."""
        from cacophony.runs.config import RunConfig
        from cacophony.runs.coordinator import Conductor
        from cacophony.schema.compiler import compile_project
        from cacophony.schema.loader import load_project

        compiled = compile_project(load_project(MULTIMODAL))
        config = RunConfig(output_dir=tmp_path, records=2, record_history=False)
        conductor = Conductor(compiled, config)
        asyncio.run(conductor.execute())
        asyncio.run(conductor.aclose())

        store = AssetStore(config.asset_root)
        recordings = [row for row in store.manifest() if row["kind"] == "audio"]
        assert recordings
        for row in recordings:
            assert row["metadata"]["transcript"]
            assert row["metadata"]["duration_seconds"] > 0
            assert Path(row["path"]).exists()
