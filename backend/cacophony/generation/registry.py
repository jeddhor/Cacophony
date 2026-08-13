"""The generator registry.

Every generation strategy in section 8 is a registered class. The registry is
the single lookup point the schema compiler uses to turn ``generator: sequence``
into a live object, and it is where plugins (section 44) will contribute their
own generators without touching Cacophony's source.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, TypeVar

from ..core.errors import GeneratorNotFoundError
from ..core.interfaces import Generator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.models import EntitySpec, FieldSpec

__all__ = ["REGISTRY", "GeneratorRegistry", "register_generator"]

GeneratorT = TypeVar("GeneratorT", bound=type[Generator])


class GeneratorRegistry:
    """A name -> generator-class mapping with alias support."""

    def __init__(self) -> None:
        self._generators: dict[str, type[Generator]] = {}
        self._aliases: dict[str, str] = {}

    # -- registration ------------------------------------------------------- #

    def register(
        self,
        name: str,
        generator_class: type[Generator],
        *,
        aliases: tuple[str, ...] = (),
        replace: bool = False,
    ) -> None:
        if name in self._generators and not replace:
            raise ValueError(f"A generator named '{name}' is already registered.")
        generator_class.name = name
        self._generators[name] = generator_class
        for alias in aliases:
            if alias in self._aliases and not replace:
                raise ValueError(f"Alias '{alias}' is already registered.")
            self._aliases[alias] = name

    def unregister(self, name: str) -> None:
        self._generators.pop(name, None)
        for alias, target in list(self._aliases.items()):
            if target == name:
                del self._aliases[alias]

    # -- lookup ------------------------------------------------------------- #

    def resolve(self, name: str) -> str:
        """Map an alias to its canonical name."""
        return self._aliases.get(name, name)

    def get(self, name: str) -> type[Generator]:
        canonical = self.resolve(name)
        try:
            return self._generators[canonical]
        except KeyError as exc:
            raise GeneratorNotFoundError(name, self.names()) from exc

    def create(
        self,
        name: str,
        options: dict[str, Any] | None = None,
        *,
        field: FieldSpec | None = None,
        entity: EntitySpec | None = None,
    ) -> Generator:
        """Instantiate a generator, running its option validation immediately."""
        return self.get(name)(options or {}, field=field, entity=entity)

    def names(self) -> list[str]:
        return sorted(self._generators)

    def aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    def describe(self) -> list[dict[str, Any]]:
        """Summarise the registry, used by ``cacophony generators``."""
        rows: list[dict[str, Any]] = []
        for name in self.names():
            generator_class = self._generators[name]
            rows.append(
                {
                    "name": name,
                    "aliases": sorted(
                        alias for alias, target in self._aliases.items() if target == name
                    ),
                    "deterministic": generator_class.deterministic,
                    "requires_provider": generator_class.requires_provider,
                    "cost_class": generator_class.cost_class,
                    "summary": _first_line(generator_class.__doc__),
                }
            )
        return rows

    def __contains__(self, name: str) -> bool:
        return self.resolve(name) in self._generators

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    def __len__(self) -> int:
        return len(self._generators)


REGISTRY = GeneratorRegistry()
"""The process-wide generator registry."""


def register_generator(
    name: str, *, aliases: tuple[str, ...] = (), registry: GeneratorRegistry | None = None
) -> Any:
    """Class decorator that registers a generator under ``name``.

    >>> @register_generator("shout")  # doctest: +SKIP
    ... class ShoutGenerator(SyncGenerator):
    ...     def generate_sync(self, context): return "HELLO"
    """

    def decorator(generator_class: GeneratorT) -> GeneratorT:
        (registry or REGISTRY).register(name, generator_class, aliases=aliases)
        return generator_class

    return decorator


def _first_line(docstring: str | None) -> str:
    if not docstring:
        return ""
    for line in docstring.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
