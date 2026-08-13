"""Generators whose backends arrive in later phases.

Section 111 is explicit: the architectural interfaces for image, speech,
scenario and plugin providers should already exist, even if their
implementations are initially empty, so that later multimodal work *extends*
the platform rather than forcing a rewrite.

These generators are therefore real, registered and compilable today. A schema
that uses ``generator: image`` validates, lints, plans and estimates correctly -
it simply cannot produce a value until the multimodal phase lands.

``llm`` used to live here. It now has a working implementation in
:mod:`cacophony.generation.generators.llm`; only the media, reference and
script generators are still waiting on their backends.

What happens at generation time follows section 65's failure-policy list:

``on_unavailable: error``        abort with an explanation (the default)
``on_unavailable: placeholder``  emit a clearly-marked deterministic stand-in
``on_unavailable: null``         emit null and carry on

``placeholder`` is what makes a forward-looking schema runnable end to end
today: the pipeline, ordering, validation and export are all exercised, and
every stand-in value is obviously a stand-in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from ...core.errors import GenerationError
from ...core.interfaces import SyncGenerator
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ...core.context import GenerationContext

__all__ = [
    "ImageGenerator",
    "PendingGenerator",
    "PlaceholderMixin",
    "ReferenceGenerator",
    "ScriptGenerator",
    "SpeechGenerator",
]


class PlaceholderMixin:
    """Deterministic stand-in values for a generator that cannot run.

    Shared with the language-model generator, which is implemented but can
    still find itself without a reachable provider. Section 65 lists
    "use placeholder" among the failure policies, and a placeholder is only
    useful if it survives the validators - so it is fitted to the field's
    declared length before being returned.
    """

    def placeholder(self, context: GenerationContext) -> Any:
        """A deterministic, obviously-synthetic stand-in value."""
        where = self.field.name if self.field else "?"  # type: ignore[attr-defined]
        return f"[{self.name}:{context.entity.name}.{where}#{context.record_index}]"  # type: ignore[attr-defined]

    def _fit(self, value: Any) -> Any:
        """Clamp a placeholder to the field's length constraints.

        A stand-in that fails validation would report a schema problem that
        does not exist, and bury the real ones. Whatever the eventual provider
        returns has to satisfy these constraints, so the placeholder does too.
        """
        field_spec = getattr(self, "field", None)
        if not isinstance(value, str) or field_spec is None:
            return value
        constraints = field_spec.constraints
        if constraints.max_length is not None and len(value) > constraints.max_length:
            value = value[: max(constraints.max_length - 1, 0)] + "…"
        if constraints.min_length is not None and len(value) < constraints.min_length:
            value = value.ljust(constraints.min_length, ".")
        return value


class PendingGenerator(OptionsMixin, PlaceholderMixin, SyncGenerator):
    """Base for generators whose backend is not implemented yet."""

    #: Which development phase implements this generator.
    phase: ClassVar[str] = "a later phase"
    #: Short human description used in the placeholder value.
    kind: ClassVar[str] = "value"

    deterministic = False

    def prepare(self) -> None:
        self.on_unavailable = self.opt_choice(
            "on_unavailable", ("error", "placeholder", "null"), "error"
        )
        self.provider = self.opt_str("provider", None)
        self.context_fields = tuple(self.opt_list("context", [], "inputs", "depends_on"))

    def dependencies(self) -> Sequence[str]:
        return self.context_fields

    def generate_sync(self, context: GenerationContext) -> Any:
        if self.on_unavailable == "null":
            return None
        if self.on_unavailable == "placeholder":
            return self._fit(self.placeholder(context))
        raise GenerationError(
            f"{context.location}: the '{self.name}' generator needs the provider backend, "
            f"which arrives in {self.phase}. Set 'on_unavailable: placeholder' to run the "
            "rest of the pipeline in the meantime."
        )

    def describe(self) -> str:
        suffix = "" if self.on_unavailable == "error" else f", {self.on_unavailable}"
        return f"{self.name}(pending{suffix})"


@register_generator("image", aliases=("invokeai", "text_to_image"))
class ImageGenerator(PendingGenerator):
    """Send a constructed prompt to an image provider (section 18)."""

    requires_provider = "image"
    cost_class = "gpu"
    phase = "the multimodal phase"
    kind = "image"

    def prepare(self) -> None:
        super().prepare()
        self.width = self.opt_int("width", 512)
        self.height = self.opt_int("height", 512)
        self.workflow = self.opt_str("workflow", None)
        self.steps = self.opt_int("steps", 30)

    def placeholder(self, context: GenerationContext) -> Any:
        return f"assets/{context.entity.name}/placeholder_{context.record_index:08d}.png"


@register_generator("tts", aliases=("speech", "voice"))
class SpeechGenerator(PendingGenerator):
    """Generate audio from generated text (section 20)."""

    requires_provider = "speech"
    cost_class = "gpu"
    phase = "the multimodal phase"
    kind = "audio"

    def prepare(self) -> None:
        super().prepare()
        self.voice = self.opt_str("voice", None)
        self.source = self.opt_str("source", None, "text", "from")

    def dependencies(self) -> Sequence[str]:
        base = tuple(super().dependencies())
        return (*base, self.source) if self.source else base

    def placeholder(self, context: GenerationContext) -> Any:
        return f"assets/{context.entity.name}/placeholder_{context.record_index:08d}.wav"


@register_generator("reference", aliases=("fk", "foreign_key", "belongs_to"))
class ReferenceGenerator(PendingGenerator):
    """A foreign-key-style reference to another entity (section 8).

    Options:
        ``entity``  the referenced entity
        ``field``   the referenced field (defaults to its primary key)

    Real foreign-key generation needs a materialised key pool for the target
    entity, which is the relational phase's job (section 91). The declaration
    already affects entity ordering today, so a schema written now compiles to
    the correct topological order.
    """

    phase = "the relational phase"
    kind = "reference"
    deterministic = True

    def prepare(self) -> None:
        super().prepare()
        target = self.opt_str("entity", None, "references", "to")
        if target is None:
            raise self._fail("option 'entity' is required")
        self.target: str = target
        self.target_field = self.opt_str("field", None, "key")

    def placeholder(self, context: GenerationContext) -> Any:
        return f"[ref:{self.target}#{context.record_index}]"

    def describe(self) -> str:
        target = self.target + (f".{self.target_field}" if self.target_field else "")
        return f"reference({target}, pending)"


@register_generator("script", aliases=("python", "custom"))
class ScriptGenerator(PendingGenerator):
    """A user-provided generator run in an isolated environment (section 8).

    Sandboxing is the whole difficulty here, and doing it badly is worse than
    not doing it at all: a project file is something people share, and an
    unsandboxed ``script:`` field would make opening one equivalent to running
    a stranger's code. The ``expression`` generator covers derived values
    safely today; this stays unimplemented until the isolation is real.
    """

    phase = "the plugin phase"
    kind = "script"

    def prepare(self) -> None:
        super().prepare()
        self.language = self.opt_choice("language", ("python", "javascript"), "python")
        self.source = self.opt_str("code", None, "script", "source")
        self.path = self.opt_str("path", None, "file")
        if not self.source and not self.path:
            raise self._fail("either 'code' or 'path' is required")
