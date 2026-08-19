"""Generators that compile but cannot run, and the machinery for saying so.

Section 111 asked that the architectural interfaces for image, speech, scenario
and plugin providers exist from the beginning, even where the implementations
were initially empty, so that later work *extended* the platform rather than
forcing a rewrite. That is what this module was for.

Only ``script`` is left, and it is a different case: it waits on isolation
rather than on a backend, which is a decision rather than a schedule. A
``script`` field still validates, lints, plans and estimates, and
``on_unavailable: placeholder`` runs the whole pipeline with a marked stand-in.

``llm``, ``reference``, ``image``, ``tts`` and ``document`` used to live here.
All now have working implementations - in
:mod:`cacophony.generation.generators.llm`,
:mod:`~cacophony.generation.generators.reference` and
:mod:`~cacophony.generation.generators.media` - leaving only ``script``, which
waits on isolation rather than on a backend.

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

__all__ = ["PendingGenerator", "PlaceholderMixin", "ScriptGenerator"]


class PlaceholderMixin:
    """Deterministic stand-in values for a generator that cannot run.

    Shared with the language-model generator, which is implemented but can
    still find itself without a reachable provider. Section 65 lists
    "use placeholder" among the failure policies, and a placeholder is only
    useful if it survives the validators - so it is fitted to the field's
    declared length before being returned.
    """

    def placeholder(self, context: GenerationContext) -> Any:
        """A deterministic, obviously-synthetic stand-in, fitted to the field.

        The fitting happens here rather than at the call sites, because there
        are two of them - one field at a time, and a whole enrichment group -
        and only one remembered. A ``max_length: 90`` helpdesk subject was
        getting a 109-character stand-in, which validation then reported as a
        schema problem that did not exist.
        """
        return self._fit(self.raw_placeholder(context))

    def raw_placeholder(self, context: GenerationContext) -> Any:
        """The stand-in before it is fitted. Subclasses override this one."""
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
    #: The whole refusal sentence, for a generator whose absence is a decision
    #: rather than a schedule. ``script`` is the case: saying it "arrives in a
    #: later phase" would be a promise the plugin phase deliberately did not
    #: make.
    refusal: ClassVar[str] = ""
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
        if self.refusal:
            raise GenerationError(f"{context.location}: {self.refusal}")
        raise GenerationError(
            f"{context.location}: the '{self.name}' generator needs the provider backend, "
            f"which arrives in {self.phase}. Set 'on_unavailable: placeholder' to run the "
            "rest of the pipeline in the meantime."
        )

    def describe(self) -> str:
        suffix = "" if self.on_unavailable == "error" else f", {self.on_unavailable}"
        return f"{self.name}(pending{suffix})"


@register_generator("script", aliases=("python", "custom"))
class ScriptGenerator(PendingGenerator):
    """A user-provided generator run in an isolated environment (section 8).

    **Still declared and still refused, and this is now a decision rather than a
    postponement.** The plugin phase examined the options and concluded that a
    real sandbox is not affordable here, so ``script`` is not shipping. The
    reasoning, recorded because a future reader will want to reopen it:

    *The requirement is absolute.* A project file is something people share - by
    email, in a Git repository, inside a ``.cacophony`` bundle. If a ``script:``
    field ran, opening a schema somebody sent you would be equivalent to running
    their code, and every other safety property in the platform would be
    decoration: the expression evaluator's allow-list, the bundle importer's
    refusal of path traversal, the plugin loader's insistence on entry points.

    *A restricted interpreter is not a sandbox.* Stripping ``__builtins__`` and
    denying imports is a denylist, and denylists on a language with
    introspection are routinely escaped through object graphs nobody thought
    about. Shipping one would invite exactly the trust it cannot support.

    *What isolation is actually available was measured, not assumed.*
    Unprivileged user and network namespaces do work on this Linux host - a
    subprocess in one cannot open a socket. But the filesystem remains fully
    readable, and blocking it needs a mount namespace and a pivot into an empty
    root: Linux-only machinery, untestable on the macOS and Windows the desktop
    phase targets. A security boundary that exists on one of three platforms is
    not a security boundary; it is a false sense of one.

    *WebAssembly is the honest option and is out of scope here.* A CPython build
    for a WASM runtime gives real isolation - no filesystem, no sockets, no host
    imports, with memory and fuel ceilings enforced by the runtime rather than by
    a list. It is also a multi-megabyte dependency and a substantial piece of
    work, and it is the right way to do this when it is done.

    **What to use instead**, in ascending order of power:

    ``expression``
        Derived values over the record, with the same allow-list, no ``eval``
        and no imports. Covers most of what a ``script`` field is reached for.

    ``patches``
        The same expressions and section 105's operations, applied per record
        during generation (section 104).

    A plugin
        Arbitrary Python, in a package somebody chose to ``pip install``
        (section 44). The trust decision sits with a person at install time
        rather than with a program at open time, which is the whole difference.

    A field using ``script`` still compiles, lints, plans and estimates, and
    ``on_unavailable: placeholder`` runs the pipeline with a marked stand-in.
    """

    phase = "no scheduled phase - see the class docstring"
    refusal = (
        "the 'script' generator is deliberately not implemented. Running code from a "
        "project file would make opening a schema somebody sent you equivalent to running "
        "their code, and no sandbox available here is trustworthy on every platform "
        "Cacophony targets (design document sections 8, 44). Use 'expression' for a "
        "derived value, 'patches' to transform one, or a plugin for real code - a plugin "
        "is a package you chose to install, which is where that decision belongs. "
        "'on_unavailable: placeholder' runs the pipeline with a marked stand-in."
    )
    kind = "script"

    def prepare(self) -> None:
        super().prepare()
        self.language = self.opt_choice("language", ("python", "javascript"), "python")
        self.source = self.opt_str("code", None, "script", "source")
        self.path = self.opt_str("path", None, "file")
        if not self.source and not self.path:
            raise self._fail("either 'code' or 'path' is required")
