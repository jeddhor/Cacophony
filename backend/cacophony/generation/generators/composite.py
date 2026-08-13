"""Composite generation (design document section 8).

Run several generators in sequence, each one seeing what the previous produced::

    generator:
      type: composite
      steps:
        - {type: faker, provider: catch_phrase}
        - {type: transform, operations: [upper]}

Section 8's motivating example is an LLM writing a biography, a rule processor
stripping prohibited content, a validator checking length and the LLM retrying
if invalid. The first and last of those arrive with the provider phase; the
chaining mechanism is here now so they slot in without changing this class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.interfaces import GeneratedValue, Generator, SyncGenerator
from ..registry import REGISTRY, register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ...core.context import GenerationContext

__all__ = ["CompositeGenerator", "TransformGenerator"]

#: Key under which a step's input value is exposed to the next step.
PIPE_KEY = "__composite_input__"


@register_generator("composite", aliases=("pipeline", "chain"))
class CompositeGenerator(OptionsMixin, Generator):
    """Run a list of generators in order, threading the value through.

    Options:
        ``steps``  a list of generator specifications

    Each step reads its predecessor's output from ``context.extras`` under
    :data:`PIPE_KEY`, so a step that ignores it (a plain Faker call, say)
    simply overwrites the value, while a step that consumes it (a transform)
    refines it.
    """

    deterministic = True

    def prepare(self) -> None:
        raw_steps = self.opt_list("steps", [], "generators", "pipeline")
        if not raw_steps:
            raise self._fail("option 'steps' must list at least one generator")

        self.steps: list[Generator] = []
        for index, step in enumerate(raw_steps):
            if isinstance(step, str):
                name, options = step, {}
            elif isinstance(step, dict):
                payload = dict(step)
                name = payload.pop("type", None) or payload.pop("generator", None)
                if not name:
                    raise self._fail(f"steps[{index}] needs a 'type'")
                options = {**payload.pop("options", {}), **payload}
            else:
                raise self._fail(f"steps[{index}] must be a string or a mapping")
            self.steps.append(
                REGISTRY.create(str(name), options, field=self.field, entity=self.entity)
            )

        self.deterministic = all(type(step).deterministic for step in self.steps)

    def dependencies(self) -> Sequence[str]:
        seen: dict[str, None] = {}
        for step in self.steps:
            for dependency in step.dependencies():
                seen[dependency] = None
        return tuple(seen)

    async def generate(self, context: GenerationContext) -> GeneratedValue:
        value: Any = None
        assets = []
        for index, step in enumerate(self.steps):
            step_context = context.sub_context(f"step{index}")
            step_context.extras[PIPE_KEY] = value
            if isinstance(step, SyncGenerator):
                value = step.generate_sync(step_context)
            else:
                produced = await step.generate(step_context)
                value = produced.value
                assets.extend(produced.assets)
        context.extras.pop(PIPE_KEY, None)
        return GeneratedValue(value=value, assets=assets)

    def describe(self) -> str:
        return "composite(" + " -> ".join(step.describe() for step in self.steps) + ")"


@register_generator("transform", aliases=("post_process",))
class TransformGenerator(OptionsMixin, SyncGenerator):
    """Apply transformations to a value (design document section 105).

    Used as a composite step, or standalone with ``source: <field>``.

    Options:
        ``operations``  a list of transform names
        ``source``      read from this field instead of the composite pipeline

    Available operations: ``lowercase``, ``uppercase``, ``title``, ``strip``,
    ``truncate:N``, ``hash``, ``mask``, ``normalize``, ``round:N``, ``slug``.
    """

    _OPERATIONS: dict[str, Any] = {
        "lowercase": lambda value, _arg: str(value).lower(),
        "uppercase": lambda value, _arg: str(value).upper(),
        "title": lambda value, _arg: str(value).title(),
        "strip": lambda value, _arg: str(value).strip(),
        "truncate": lambda value, arg: str(value)[: int(arg or 50)],
        "normalize": lambda value, _arg: " ".join(str(value).split()),
        "round": lambda value, arg: round(float(value), int(arg or 0)),
    }

    def prepare(self) -> None:
        raw = self.opt_list("operations", [], "ops", "transform")
        if not raw:
            raise self._fail("option 'operations' must list at least one transformation")

        self.source = self.opt_str("source", None, "from", "field")
        self.operations: list[tuple[str, str | None]] = []
        for item in raw:
            name, _, argument = str(item).partition(":")
            if name in ("hash", "mask", "slug") or name in self._OPERATIONS:
                self.operations.append((name, argument or None))
            else:
                known = ", ".join([*sorted(self._OPERATIONS), "hash", "mask", "slug"])
                raise self._fail(f"unknown transformation '{name}'. Available: {known}")

    def dependencies(self) -> Sequence[str]:
        return (self.source,) if self.source else ()

    def generate_sync(self, context: GenerationContext) -> Any:
        if self.source:
            value: Any = context.value(self.source)
        else:
            value = context.extras.get(PIPE_KEY)

        for name, argument in self.operations:
            if value is None:
                return None
            if name == "hash":
                import hashlib

                value = hashlib.blake2b(str(value).encode("utf-8"), digest_size=16).hexdigest()[
                    : int(argument or 32)
                ]
            elif name == "mask":
                text = str(value)
                keep = int(argument or 4)
                value = "*" * max(len(text) - keep, 0) + text[-keep:] if keep else "*" * len(text)
            elif name == "slug":
                import re

                value = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
            else:
                value = self._OPERATIONS[name](value, argument)
        return value

    def describe(self) -> str:
        return "transform(" + ", ".join(name for name, _ in self.operations) + ")"
