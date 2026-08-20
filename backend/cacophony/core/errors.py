"""Cacophony's exception hierarchy.

Errors are split by *who* is at fault, because the CLI and (later) the API
render them differently:

* :class:`SchemaError` and subclasses  - the user's project is wrong.
* :class:`GenerationError` and subclasses - something failed while generating.
* :class:`ProviderError` and subclasses - an external backend misbehaved.
"""

from __future__ import annotations

__all__ = [
    "CacophonyError",
    "CircularDependencyError",
    "GenerationError",
    "GeneratorConfigError",
    "GeneratorNotFoundError",
    "OutputError",
    "ProviderError",
    "ProviderNotFoundError",
    "ProviderUnavailableError",
    "SchemaError",
    "UnknownFieldReferenceError",
    "ValidationFailedError",
]


class CacophonyError(Exception):
    """Base class for every error raised by Cacophony."""


# --------------------------------------------------------------------------- #
# Schema-time errors
# --------------------------------------------------------------------------- #


class SchemaError(CacophonyError):
    """The project schema is invalid and cannot be compiled."""


class UnknownFieldReferenceError(SchemaError):
    """A field depends on something that does not exist in the schema."""

    def __init__(self, entity: str, field: str, reference: str) -> None:
        self.entity = entity
        self.field = field
        self.reference = reference
        super().__init__(
            f"{entity}.{field} depends on '{reference}', which is not defined "
            f"in entity '{entity}' and is not a known entity reference."
        )


class CircularDependencyError(SchemaError):
    """A dependency cycle prevents an ordering from being computed.

    Section 100 of the design document requires that circular dependency errors
    be surfaced clearly, so the cycle itself is part of the message.
    """

    def __init__(self, cycle: list[str], kind: str = "field") -> None:
        self.cycle = cycle
        self.kind = kind
        arrow = " -> ".join([*cycle, cycle[0]]) if cycle else "<empty>"
        super().__init__(f"Circular {kind} dependency detected: {arrow}")


class GeneratorNotFoundError(SchemaError):
    """No generator is registered under the requested name."""

    def __init__(self, name: str, available: list[str] | None = None) -> None:
        self.name = name
        self.available = available or []
        hint = f" Available generators: {', '.join(sorted(self.available))}." if available else ""
        super().__init__(f"Unknown generator '{name}'.{hint}")


class GeneratorConfigError(SchemaError):
    """A generator was given options it cannot honour."""

    def __init__(self, generator: str, message: str, *, location: str | None = None) -> None:
        self.generator = generator
        self.location = location
        where = f" at {location}" if location else ""
        super().__init__(f"Generator '{generator}'{where}: {message}")


# --------------------------------------------------------------------------- #
# Generation-time errors
# --------------------------------------------------------------------------- #


class GenerationError(CacophonyError):
    """A value could not be produced."""


class ValidationFailedError(CacophonyError):
    """A generated record failed validation and the policy is to abort."""


class PathNotAllowedError(CacophonyError):
    """A request named a path outside the directories the server was given.

    Only ever raised when the server was started with roots - which the CLI
    requires for any bind beyond loopback. On loopback the API is as powerful as
    the shell that started it, and confining it there would be theatre.
    """


class OutputError(CacophonyError):
    """An output writer could not open, write, or close its destination."""


# --------------------------------------------------------------------------- #
# Provider errors (used from the provider phase onwards)
# --------------------------------------------------------------------------- #


class ProviderError(CacophonyError):
    """An external generation backend failed."""


class ProviderNotFoundError(ProviderError):
    """No provider is registered under the requested id."""


class ProviderUnavailableError(ProviderError):
    """The provider is configured but not reachable or not healthy."""
