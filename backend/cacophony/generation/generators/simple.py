"""Constant, sequence and identifier generators (design document section 8)."""

from __future__ import annotations

import re
import uuid as _uuid
from typing import TYPE_CHECKING, Any

from ...core.interfaces import SyncGenerator
from ...core.types import DataType
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...core.context import GenerationContext

__all__ = ["ConstantGenerator", "NullGenerator", "SequenceGenerator", "UuidGenerator"]

#: Matches the zero-padding placeholder in ``"EMP-{000000}"``.
_PAD_TOKEN = re.compile(r"\{(0*|#*|)\}")


@register_generator("constant", aliases=("literal", "fixed"))
class ConstantGenerator(OptionsMixin, SyncGenerator):
    """Always produce the same value."""

    def prepare(self) -> None:
        self.value = self.opt("value", None, "constant")

    def generate_sync(self, context: GenerationContext) -> Any:
        return self.value

    def describe(self) -> str:
        return f"constant({self.value!r})"


@register_generator("null", aliases=("none", "empty"))
class NullGenerator(OptionsMixin, SyncGenerator):
    """Always produce null. Useful for placeholder fields and negative tests."""

    def generate_sync(self, context: GenerationContext) -> Any:
        return None


@register_generator("sequence", aliases=("serial", "autoincrement"))
class SequenceGenerator(OptionsMixin, SyncGenerator):
    """Produce ``1, 2, 3`` or ``USER-000001, USER-000002`` (section 8).

    Options:
        ``format``  template containing a padding token, e.g. ``"EMP-{000000}"``
        ``start``   first value (default ``1``)
        ``step``    increment (default ``1``)
        ``pad``     zero-padding width when no ``format`` is given

    The value is derived from ``record_index`` rather than from an internal
    counter, so record 4,823,913 has the same id whether it is produced first
    or last. That is what makes parallel and resumed runs reproducible
    (sections 30, 32 and 75).
    """

    def prepare(self) -> None:
        self.format: str | None = self.opt_str("format", None, "template", "pattern")
        # Explicit defaults rather than `or`: `start: 0` and `step: 0` are
        # values a user can legitimately write, and `x or default` would
        # silently rewrite both of them.
        start = self.opt_int("start", 1)
        step = self.opt_int("step", 1)
        pad = self.opt_int("pad", 0)
        self.start: int = 1 if start is None else start
        self.step: int = 1 if step is None else step
        self.pad: int = 0 if pad is None else pad

        if self.step == 0:
            raise self._fail("option 'step' must not be zero")

        self._prefix = ""
        self._suffix = ""
        if self.format is not None:
            match = _PAD_TOKEN.search(self.format)
            if match is None:
                raise self._fail(
                    f"format {self.format!r} contains no padding token. "
                    'Use something like "EMP-{000000}".'
                )
            self.pad = max(self.pad, len(match.group(1)))
            self._prefix = self.format[: match.start()]
            self._suffix = self.format[match.end() :]

    def generate_sync(self, context: GenerationContext) -> Any:
        number = self.start + context.record_index * self.step
        if self.format is None and self.pad == 0:
            if self.field is not None and self.field.type.is_textual:
                return str(number)
            return number
        return f"{self._prefix}{number:0{self.pad}d}{self._suffix}"

    def describe(self) -> str:
        return f"sequence({self.format})" if self.format else "sequence"


@register_generator("uuid", aliases=("guid",))
class UuidGenerator(OptionsMixin, SyncGenerator):
    """Produce a UUID that is stable for a given seed.

    Options:
        ``version``  ``4`` (default) or ``5``
        ``namespace``/``name``  for version 5

    A version-4 UUID is normally unpredictable, which would break
    reproducibility. Here the 128 bits are drawn from the field's seed instead,
    so the value is random-looking but deterministic.
    """

    def prepare(self) -> None:
        self.version = self.opt_int("version", 4) or 4
        if self.version not in (4, 5):
            raise self._fail("option 'version' must be 4 or 5")
        self.as_string = self.opt_bool("string", True)
        if self.version == 5:
            namespace = self.opt_str("namespace", str(_uuid.NAMESPACE_URL))
            try:
                self._namespace = _uuid.UUID(str(namespace))
            except ValueError as exc:
                raise self._fail(f"option 'namespace' is not a valid UUID: {namespace!r}") from exc
            self._name_field = self.opt_str("name", None)

    def dependencies(self) -> tuple[str, ...]:
        if self.version == 5 and getattr(self, "_name_field", None):
            return (self._name_field,)  # type: ignore[return-value]
        return ()

    def generate_sync(self, context: GenerationContext) -> Any:
        if self.version == 5:
            name = (
                str(context.value(self._name_field))
                if self._name_field
                else f"{context.entity.name}:{context.record_index}"
            )
            value = _uuid.uuid5(self._namespace, name)
        else:
            value = _uuid.UUID(int=context.rng().getrandbits(128), version=4)
        if self.as_string or (self.field is not None and self.field.type is not DataType.UUID):
            return str(value)
        return value
