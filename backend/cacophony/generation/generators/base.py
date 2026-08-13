"""Shared plumbing for generator implementations.

Option reading lives here so that every generator reports a bad option the same
way, and so that mistakes surface at compile time (``cacophony validate``)
rather than mid-run.
"""

from __future__ import annotations

from typing import Any

from ...core.errors import GeneratorConfigError
from ...core.interfaces import Generator

__all__ = ["OptionsMixin"]

_MISSING = object()

_TRUE = {"true", "t", "yes", "y", "1", "on"}
_FALSE = {"false", "f", "no", "n", "0", "off"}


class OptionsMixin(Generator):
    """Typed, alias-aware access to a generator's option bag."""

    @property
    def _where(self) -> str | None:
        if self.entity is not None and self.field is not None:
            return f"{self.entity.name}.{self.field.name}"
        if self.field is not None:
            return self.field.name
        return None

    def _fail(self, message: str) -> GeneratorConfigError:
        return GeneratorConfigError(self.name or type(self).__name__, message, location=self._where)

    # -- raw access --------------------------------------------------------- #

    def opt(self, key: str, default: Any = None, *aliases: str) -> Any:
        """Read an option, falling back through ``aliases`` then ``default``."""
        for candidate in (key, *aliases):
            if candidate in self.options and self.options[candidate] is not None:
                return self.options[candidate]
        return default

    def require(self, key: str, *aliases: str) -> Any:
        value = self.opt(key, _MISSING, *aliases)
        if value is _MISSING:
            named = "' or '".join((key, *aliases))
            raise self._fail(f"option '{named}' is required")
        return value

    # -- typed access ------------------------------------------------------- #

    def opt_int(self, key: str, default: int | None = None, *aliases: str) -> int | None:
        value = self.opt(key, None, *aliases)
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise self._fail(f"option '{key}' must be an integer, got {value!r}") from exc

    def opt_float(self, key: str, default: float | None = None, *aliases: str) -> float | None:
        value = self.opt(key, None, *aliases)
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise self._fail(f"option '{key}' must be a number, got {value!r}") from exc

    def opt_str(self, key: str, default: str | None = None, *aliases: str) -> str | None:
        value = self.opt(key, None, *aliases)
        return default if value is None else str(value)

    def opt_bool(self, key: str, default: bool = False, *aliases: str) -> bool:
        value = self.opt(key, None, *aliases)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise self._fail(f"option '{key}' must be a boolean, got {value!r}")

    def opt_list(self, key: str, default: list[Any] | None = None, *aliases: str) -> list[Any]:
        value = self.opt(key, None, *aliases)
        if value is None:
            return list(default or [])
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        raise self._fail(f"option '{key}' must be a list, got {type(value).__name__}")

    def opt_choice(self, key: str, allowed: tuple[str, ...], default: str) -> str:
        value = str(self.opt(key, default))
        if value not in allowed:
            raise self._fail(f"option '{key}' must be one of {', '.join(allowed)}, got {value!r}")
        return value
