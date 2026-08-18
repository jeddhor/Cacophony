"""Asking a language model for a schema (design document sections 50, 110).

Shared by ``cacophony propose``, which stops once it has one, and
``cacophony begin``, which carries on to generate it. The provider handling
lives here so the two commands cannot drift: a description that produces a
schema from one of them produces the same schema from the other.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ..core.errors import CacophonyError
from .theme import console, error_console

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.models import ProviderSpec

__all__ = ["ask_for_a_schema", "provider_spec_for"]


def provider_spec_for(
    *,
    provider_from: Path | None,
    adapter: str,
    base_url: str | None,
    model: str | None,
) -> ProviderSpec:
    """Which language model to ask: one borrowed from a project, or one described.

    Borrowing is the common case. A project that already talks to a model has
    the URL, the model name and any secret worked out, and asking the same
    server for a schema as will later write the prose keeps one thing
    configured instead of two.
    """
    from ..schema.loader import load_project
    from ..schema.models import ProviderSpec

    if provider_from is None:
        return ProviderSpec(
            id="assistant",
            type="language_model",
            adapter=adapter,
            base_url=base_url,
            model=model,
        )

    try:
        borrowed = load_project(provider_from)
    except CacophonyError as exc:
        error_console.print(f"[cacophony.error]error[/] {exc}")
        raise typer.Exit(code=2) from exc

    specs = [spec for spec in borrowed.providers.values() if spec.type == "language_model"]
    if not specs:
        error_console.print(
            f"[cacophony.error]error[/] {provider_from} configures no language model"
        )
        raise typer.Exit(code=2)

    spec = specs[0]
    return spec.model_copy(update={"model": model}) if model else spec


def ask_for_a_schema(
    spec: ProviderSpec,
    description: str,
    *,
    seed: int | None = None,
    scale: int | None = None,
) -> Any:
    """Ask, and return the proposal, or exit with a message worth reading.

    The model proposes entities, fields and relationships; Cacophony picks the
    generators, compiles the result and lints it before anybody sees it. What
    comes back is therefore a schema that is known to work rather than a
    plausible-looking one.
    """
    from ..providers.base import LanguageModelProvider
    from ..providers.registry import PROVIDER_REGISTRY
    from ..schema.assistant import SchemaAssistant, SchemaProposalError

    try:
        provider = PROVIDER_REGISTRY.create(spec)
    except CacophonyError as exc:
        error_console.print(f"[cacophony.error]error[/] {exc}")
        raise typer.Exit(code=2) from exc

    if not isinstance(provider, LanguageModelProvider):
        error_console.print(
            f"[cacophony.error]error[/] adapter '{spec.adapter}' is not a language model"
        )
        raise typer.Exit(code=2)

    assistant = SchemaAssistant(provider, model=spec.model)

    async def ask() -> Any:
        try:
            return await assistant.propose(description, seed=seed, scale=scale)
        finally:
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()

    with console.status("[cacophony.muted]designing…[/]", spinner="dots"):
        try:
            return asyncio.run(ask())
        except (SchemaProposalError, CacophonyError) as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=1) from exc
