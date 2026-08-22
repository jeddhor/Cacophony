"""Image, speech and document generators (design document sections 18-23, 81).

These are the first generators that produce a *file* rather than a value. The
field's value becomes the path; the bytes go to the asset store; the record
grows an asset entry carrying the provenance section 19 asks for.

Three things they share, all of which matter more than the generation itself.

**The path is known before the work.** An asset's location is derived from its
entity, record index and field (see
:mod:`cacophony.assets.store`), so a generator can ask "is this already on
disk?" and skip a thirty-second diffusion call on a resumed run. Regenerating
what already exists is the single most expensive mistake a media pipeline can
make.

**The prompt is compiled, not written.** Section 12's prompt compiler already
turns a field's meaning plus its record's context into text; an image prompt is
the same idea with a different consumer. A user writes what the picture is
*of*, and the record supplies who it is of.

**Failure is a policy, not a crash.** Section 65 lists retry, skip, placeholder,
incomplete and abort. A portrait that fails on record 4,823,913 must not
destroy the run, so ``on_unavailable`` and the engine's failure policy both
apply exactly as they do to a language-model field.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from ...core.errors import GenerationError, ProviderError
from ...core.interfaces import GeneratedValue, Generator
from ...core.provenance import FieldProvenance
from ...core.record import GeneratedAsset
from ..registry import register_generator
from .base import OptionsMixin
from .deferred import PlaceholderMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ...assets.store import AssetStore
    from ...core.context import GenerationContext

__all__ = ["DocumentGenerator", "ImageGenerator", "SoundGenerator", "SpeechGenerator"]


class MediaGenerator(OptionsMixin, PlaceholderMixin, Generator):
    """Shared behaviour for generators that write files."""

    deterministic = False
    cost_class = "gpu"

    #: What the asset is, for the manifest and the record.
    kind = "file"

    def prepare(self) -> None:
        self.on_unavailable = self.opt_choice(
            "on_unavailable", ("error", "placeholder", "null"), "error"
        )
        self.provider_id = self.opt_str("provider", None)
        self.context_fields = tuple(self.opt_list("context", [], "inputs"))
        #: Re-use a file that is already on disk instead of regenerating it.
        self.reuse = self.opt_bool("reuse", True)

    def dependencies(self) -> Sequence[str]:
        return self.context_fields

    # -- the pieces each subclass supplies ---------------------------------- #

    def _store(self, context: GenerationContext) -> AssetStore | None:
        return getattr(context, "assets", None)

    def _seed_for(self, context: GenerationContext) -> int:
        return context.seed & 0x7FFFFFFF

    async def _produce(self, context: GenerationContext) -> tuple[bytes, str, dict[str, Any]]:
        """Return ``(data, media_type, provenance)``."""
        raise NotImplementedError

    def _media_type(self) -> str:
        raise NotImplementedError

    # -- generation --------------------------------------------------------- #

    async def generate(self, context: GenerationContext) -> GeneratedValue:
        store = self._store(context)
        provenance = FieldProvenance(generator=self.name, seed=context.seed)

        if store is None:
            return self._degrade(
                context,
                provenance,
                f"{context.location}: this field writes a file, but no asset store is "
                "attached to the run.",
            )

        path = store.path_for(
            context.entity.name,
            context.record_index,
            self.field.name if self.field else self.name,
            media_type=self._media_type(),
        )

        # Section 65's most valuable optimisation: never pay twice. Resuming a
        # portrait-heavy run is hundreds of times faster than starting it.
        if self.reuse and not store.overwrite and store.exists(path):
            from ...assets.store import StoredAsset

            provenance.extra["reused"] = True
            field_name = self.field.name if self.field else self.name
            stored = store.note_reuse(
                StoredAsset(
                    entity=context.entity.name,
                    record_index=context.record_index,
                    field=field_name,
                    kind=self.kind,
                    path=path,
                    media_type=self._media_type(),
                    size_bytes=path.stat().st_size,
                    digest="",
                    record_id=str(context.current_record.get(_primary_key(context)) or ""),
                    metadata={"reused": True},
                )
            )
            asset = stored.to_asset()
            return GeneratedValue(
                value=self._value_for(asset, store), assets=[asset], provenance=provenance
            )

        try:
            data, media_type, extra = await self._produce(context)
        except ProviderError as exc:
            return self._degrade(context, provenance, str(exc))
        except NotImplementedError:  # pragma: no cover - a subclass bug, not a run failure
            raise

        stored = store.write(
            data,
            entity=context.entity.name,
            record_index=context.record_index,
            field_name=self.field.name if self.field else self.name,
            kind=self.kind,
            media_type=media_type,
            record_id=str(context.current_record.get(_primary_key(context)) or ""),
            metadata=extra,
            # This field was generated rather than skipped, so its bytes are
            # the current ones; keeping an older file would be arbitrary.
            overwrite=True if not self.reuse else None,
        )
        provenance.extra.update(extra)

        asset = stored.to_asset()
        return GeneratedValue(
            value=self._value_for(asset, store), assets=[asset], provenance=provenance
        )

    def _value_for(self, asset: GeneratedAsset, store: AssetStore) -> Any:
        """What lands in the record: a path relative to the output directory."""
        try:
            return str(asset.path.relative_to(store.root.parent))
        except ValueError:
            return str(asset.path)

    def _degrade(
        self, context: GenerationContext, provenance: FieldProvenance, reason: str
    ) -> GeneratedValue:
        if self.on_unavailable == "null":
            provenance.extra["unavailable"] = reason
            return GeneratedValue(value=None, provenance=provenance)
        if self.on_unavailable == "placeholder":
            provenance.extra["unavailable"] = reason
            return GeneratedValue(value=self._fit(self.placeholder(context)), provenance=provenance)
        raise GenerationError(reason)

    def describe(self) -> str:
        return f"{self.name}({self.provider_id or 'default provider'})"


def _primary_key(context: GenerationContext) -> str:
    return context.entity.resolved_primary_key() or ""


# --------------------------------------------------------------------------- #
# Images (sections 18, 19)
# --------------------------------------------------------------------------- #


@register_generator("image", aliases=("invokeai", "text_to_image"))
class ImageGenerator(MediaGenerator):
    """Send a constructed prompt to an image provider (section 18).

    Options:
        ``prompt``      template for the prompt, with ``{field}`` placeholders
        ``style``       procedural styles: identicon, portrait, card, document
        ``width`` / ``height`` / ``steps`` / ``guidance``
        ``negative_prompt``
        ``workflow``    an InvokeAI workflow name or graph
        ``provider``    which image provider, when a project declares several
        ``context``     fields the prompt may read
        ``reuse``       skip regeneration when the file already exists
    """

    requires_provider = "image"
    kind = "image"

    def prepare(self) -> None:
        super().prepare()
        self.prompt_template = self.opt_str("prompt", None, "template")
        self.style = self.opt_str("style", None)
        self.width = self.opt_int("width", 512) or 512
        self.height = self.opt_int("height", 512) or 512
        self.steps = self.opt_int("steps", None)
        self.guidance = self.opt_float("guidance", None, "cfg_scale")
        self.negative_prompt = self.opt_str("negative_prompt", None, "negative")
        self.workflow = self.opt_str("workflow", None)

    def _media_type(self) -> str:
        return "image/png"

    def dependencies(self) -> Sequence[str]:
        from .text import placeholders_in

        base = list(super().dependencies())
        if self.prompt_template:
            base.extend(placeholders_in(self.prompt_template))
        return tuple(dict.fromkeys(base))

    async def _produce(self, context: GenerationContext) -> tuple[bytes, str, dict[str, Any]]:
        from ...providers.base import ImageRequest

        runtime = getattr(context, "runtime", None)
        if runtime is None:
            raise ProviderError(f"{context.location}: no provider runtime is attached")

        provider = runtime.media_provider("image", self.provider_id)
        prompt = self._prompt_for(context)

        result = await provider.generate(
            ImageRequest(
                prompt=prompt,
                width=self.width,
                height=self.height,
                seed=self._seed_for(context),
                steps=self.steps,
                guidance=self.guidance,
                negative_prompt=self.negative_prompt,
                workflow=self.workflow,
                metadata={"style": self.style} if self.style else {},
            )
        )
        if not result.data:
            raise ProviderError(f"{context.location}: the image provider returned no data")

        # Section 19's provenance block, kept with the asset rather than in the
        # record, because it is about the file.
        return (
            result.data,
            result.media_type,
            {
                "provider": result.provider,
                "workflow": result.workflow,
                "seed": result.seed,
                "prompt_hash": result.prompt_hash
                or hashlib.blake2b(prompt.encode("utf-8"), digest_size=8).hexdigest(),
                "width": result.width or self.width,
                "height": result.height or self.height,
            },
        )

    def _prompt_for(self, context: GenerationContext) -> str:
        """What the picture is of.

        A template is filled from the record; without one, the field's meaning
        plus the record's context stands in - the same division of labour the
        prompt compiler uses for text (section 12).
        """
        from .text import fill_placeholders

        if self.prompt_template:
            return fill_placeholders(self.prompt_template, context)

        meaning = (self.field.meaning if self.field else None) or (
            f"an image for the {self.field.name if self.field else 'record'} field"
        )
        details = ", ".join(
            f"{name}: {value}"
            for name, value in list(context.current_record.items())[:8]
            if value is not None and not isinstance(value, (dict, list))
        )
        return f"{meaning}. {details}" if details else meaning

    def placeholder(self, context: GenerationContext) -> Any:
        return f"assets/{context.entity.name}/placeholder_{context.record_index:08d}.png"


# --------------------------------------------------------------------------- #
# Speech (sections 20, 21)
# --------------------------------------------------------------------------- #


@register_generator("tts", aliases=("speech", "voice"))
class SpeechGenerator(MediaGenerator):
    """Generate audio from generated text (section 20).

    Options:
        ``source``    the field holding the text to speak (required)
        ``voice``     a voice name, or a field to read one from
        ``speed`` / ``language`` / ``sample_rate``
        ``provider``  which speech provider, when a project declares several
        ``reuse``     skip regeneration when the file already exists
    """

    requires_provider = "speech"
    kind = "audio"

    def prepare(self) -> None:
        super().prepare()
        source = self.opt_str("source", None, "text", "from")
        if not source:
            raise self._fail(
                "option 'source' is required: name the field holding the text to speak"
            )
        self.source: str = source
        self.voice = self.opt_str("voice", None)
        self.voice_field = self.opt_str("voice_field", None, "voice_from")
        self.speed = self.opt_float("speed", None)
        self.language = self.opt_str("language", None)
        self.sample_rate = self.opt_int("sample_rate", None)

    def _media_type(self) -> str:
        return "audio/wav"

    def dependencies(self) -> Sequence[str]:
        base = [*super().dependencies(), self.source]
        if self.voice_field:
            base.append(self.voice_field)
        return tuple(dict.fromkeys(base))

    async def _produce(self, context: GenerationContext) -> tuple[bytes, str, dict[str, Any]]:
        from ...providers.base import SpeechRequest

        runtime = getattr(context, "runtime", None)
        if runtime is None:
            raise ProviderError(f"{context.location}: no provider runtime is attached")

        text = context.value(self.source, "")
        if text is None or not str(text).strip():
            raise ProviderError(
                f"{context.location}: '{self.source}' is empty, so there is nothing to speak"
            )

        voice = self.voice
        if self.voice_field:
            voice = str(context.value(self.voice_field, "") or "") or self.voice

        provider = runtime.media_provider("speech", self.provider_id)
        result = await provider.synthesize(
            SpeechRequest(
                text=str(text),
                voice=voice,
                language=self.language,
                speed=self.speed,
                sample_rate=self.sample_rate,
            )
        )
        if not result.data:
            raise ProviderError(f"{context.location}: the speech provider returned no audio")

        return (
            result.data,
            result.media_type,
            {
                "provider": result.provider,
                "voice": result.voice,
                "duration_seconds": result.duration_seconds,
                "sample_rate": result.sample_rate,
                "source_field": self.source,
                # An aligned transcript is what makes a speech dataset usable
                # (section 21), and it is free to record here.
                "transcript": str(text),
            },
        )

    def placeholder(self, context: GenerationContext) -> Any:
        return f"assets/{context.entity.name}/placeholder_{context.record_index:08d}.wav"


# --------------------------------------------------------------------------- #
# Non-speech audio (section 22)
# --------------------------------------------------------------------------- #


@register_generator("sound", aliases=("audio", "noise"))
class SoundGenerator(MediaGenerator):
    """Synthesise audio that is not a voice (design document section 22).

    Alarms, ambience, machine noise and notifications: the sounds a security
    or telemetry dataset is full of and a speech provider cannot make. Like the
    procedural image adapter, this needs no server and no GPU - a dataset of ten
    thousand alarms should not require an inference stack, and the section's
    point is the *variety* of audio rather than its fidelity.

    Options:
        ``kind``        alarm, ambience, machine, notification or beep
        ``kind_field``  read the kind from another field instead
        ``seconds``     length, default 2.0
        ``level``       0-1, how loud
        ``distortion``  0-1, soft clipping - what a cheap microphone does
        ``sample_rate`` default 22,050
    """

    requires_provider = None
    cost_class = "cpu"
    kind = "audio"

    def prepare(self) -> None:
        super().prepare()
        from ...assets.audio import SOUND_KINDS

        self.sound_kind = self.opt_choice("kind", tuple(sorted(SOUND_KINDS)), "beep")
        self.kind_field = self.opt_str("kind_field", None, "kind_from")
        self.seconds = self.opt_float("seconds", 2.0) or 2.0
        self.level = self.opt_float("level", 0.6) or 0.6
        self.distortion = self.opt_float("distortion", 0.0) or 0.0
        self.sample_rate = self.opt_int("sample_rate", None)

    def _media_type(self) -> str:
        return "audio/wav"

    def dependencies(self) -> Sequence[str]:
        base = [*super().dependencies()]
        if self.kind_field:
            base.append(self.kind_field)
        return tuple(dict.fromkeys(base))

    async def _produce(self, context: GenerationContext) -> tuple[bytes, str, dict[str, Any]]:
        from ...assets.audio import DEFAULT_SAMPLE_RATE, SOUND_KINDS, duration_of, sound_like

        kind = self.sound_kind
        if self.kind_field:
            named = str(context.value(self.kind_field, "") or "").strip().lower()
            if named and named not in SOUND_KINDS:
                raise self._fail(
                    f"'{self.kind_field}' holds {named!r}, which is not a sound this "
                    f"generator makes. Available: {', '.join(sorted(SOUND_KINDS))}"
                )
            kind = named or kind

        data = sound_like(
            kind,
            seconds=self.seconds,
            seed=self._seed_for(context),
            sample_rate=self.sample_rate or DEFAULT_SAMPLE_RATE,
            level=self.level,
            distortion=self.distortion,
        )
        return (
            data,
            "audio/wav",
            {
                "provider": "procedural",
                "sound": kind,
                "duration_seconds": duration_of(data),
                "sample_rate": self.sample_rate or DEFAULT_SAMPLE_RATE,
                # Said in the manifest, not only in the manual: this is
                # synthesised audio and nothing about it is a recording.
                "synthetic": True,
            },
        )

    def placeholder(self, context: GenerationContext) -> Any:
        return f"assets/{context.entity.name}/placeholder_{context.record_index:08d}.wav"


# --------------------------------------------------------------------------- #
# Documents (section 23)
# --------------------------------------------------------------------------- #


@register_generator("document", aliases=("pdf", "invoice", "report"))
class DocumentGenerator(MediaGenerator):
    """Render a record as a document (section 23).

    Options:
        ``template``    the document body, with ``{field}`` placeholders
        ``template_path`` read the template from a file instead
        ``format``      ``pdf`` (default), ``html``, ``txt``
        ``title``       a title template
        ``page_size``   a4, a5, letter, legal
        ``font`` / ``font_size``

    Needs no provider: a document is rendered from the record it describes, so
    this is the one media generator that works on any machine with nothing
    configured.
    """

    requires_provider = None
    cost_class = "cpu"
    kind = "document"

    #: Output format to media type.
    _MEDIA_TYPES = {"pdf": "application/pdf", "html": "text/html", "txt": "text/plain"}

    def prepare(self) -> None:
        super().prepare()
        self.format = self.opt_choice("format", ("pdf", "html", "txt"), "pdf")
        self.template = self.opt_str("template", None, "body")
        self.template_path = self.opt_str("template_path", None, "path")
        if not self.template and not self.template_path:
            raise self._fail("either 'template' or 'template_path' is required")
        self.title_template = self.opt_str("title", None)
        self.page_size = self.opt_str("page_size", "a4") or "a4"
        self.font = self.opt_str("font", "helvetica") or "helvetica"
        self.font_size = self.opt_float("font_size", 11.0) or 11.0
        self._loaded: str | None = None

    def _media_type(self) -> str:
        return self._MEDIA_TYPES[self.format]

    def dependencies(self) -> Sequence[str]:
        from .text import placeholders_in

        base = list(super().dependencies())
        base.extend(placeholders_in(self._body()))
        if self.title_template:
            base.extend(placeholders_in(self.title_template))
        return tuple(dict.fromkeys(base))

    def _body(self) -> str:
        if self.template is not None:
            return self.template
        if self._loaded is None:
            # Through `resolve_path` like every other schema-named file: it
            # finds the template beside the project, and it is where the path
            # policy a confined server installed gets consulted.
            path = self.resolve_path(self.template_path or "")
            try:
                self._loaded = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise self._fail(f"could not read the template at {path}: {exc}") from exc
        return self._loaded

    async def _produce(self, context: GenerationContext) -> tuple[bytes, str, dict[str, Any]]:
        from ...assets.documents import Document, render_template

        values = dict(context.current_record)
        related = {name: dict(record.values) for name, record in context.related_records.items()}
        text = render_template(self._body(), values, related=related)
        title = (
            render_template(self.title_template, values, related=related)
            if self.title_template
            else ""
        )

        if self.format == "txt":
            data = text.encode("utf-8")
        elif self.format == "html":
            data = _as_html(title, text).encode("utf-8")
        else:
            document = Document(
                title=title,
                page_size=self.page_size,
                font=self.font,
                size=self.font_size,
            ).layout(text)
            data = document.to_pdf()

        return (
            data,
            self._media_type(),
            {"format": self.format, "title": title, "characters": len(text)},
        )

    def placeholder(self, context: GenerationContext) -> Any:
        suffix = {"pdf": "pdf", "html": "html", "txt": "txt"}[self.format]
        return f"assets/{context.entity.name}/placeholder_{context.record_index:08d}.{suffix}"

    def describe(self) -> str:
        return f"document({self.format})"


def _as_html(title: str, text: str) -> str:
    """A minimal, self-contained HTML document.

    Everything is escaped: a generated value containing ``<script>`` is a
    string, not markup, and a synthetic dataset that quietly produces working
    HTML injection is a liability rather than a feature.
    """
    from html import escape

    paragraphs = "\n".join(
        f"    <p>{escape(line)}</p>" for line in text.split("\n") if line.strip()
    )
    return (
        "<!doctype html>\n<html>\n  <head>\n"
        f'    <meta charset="utf-8">\n    <title>{escape(title)}</title>\n'
        "  </head>\n  <body>\n"
        f"{paragraphs}\n"
        "  </body>\n</html>\n"
    )
