"""What a worker can do, and what a shard needs (design document sections 84, 95).

    Cacophony Controller
          ├── CPU Worker Node
          ├── LLM GPU Node
          ├── InvokeAI Node
          ├── TTS Node
          └── Export Node

    Workers advertise capabilities. The scheduler routes jobs appropriately.

A capability is a claim a worker makes about itself: "I can run deterministic
generators", "I have a language model at this URI", "I have a GPU with a
diffusion model on it". A shard carries the set it *needs*, derived from the
entity's generators rather than declared by anyone.

Matching is deliberately one-directional and conservative. A worker may
advertise more than a shard needs; it may never advertise less. Handing an
image shard to a node with no image provider does not produce a slower run, it
produces a run that fails on its first record - after the scheduler has already
told the controller it was in hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from ..schema.plan import CompiledEntity

__all__ = ["CAPABILITIES", "Capabilities", "capabilities_for"]

#: Every capability the scheduler knows how to route on.
#:
#: ``deterministic`` is what every worker has: Faker, sequences, expressions,
#: references, timelines - the whole of Cacophony that needs no backend. The
#: rest name a *provider kind*, so a worker advertising ``image`` is claiming a
#: configured image provider rather than a graphics card.
CAPABILITIES = ("deterministic", "language_model", "image", "speech", "document")


@dataclass(frozen=True, slots=True)
class Capabilities:
    """A set of capability names, with the matching rule attached."""

    names: frozenset[str] = frozenset({"deterministic"})

    @classmethod
    def of(cls, names: Iterable[str]) -> Capabilities:
        cleaned = {str(name).strip().lower() for name in names if str(name).strip()}
        unknown = cleaned - set(CAPABILITIES)
        if unknown:
            known = ", ".join(CAPABILITIES)
            raise ValueError(
                f"unknown capabilities: {', '.join(sorted(unknown))}. Available: {known}"
            )
        # Everyone can run deterministic generators; saying so explicitly keeps
        # the matching rule from having to special-case the common shard.
        return cls(frozenset(cleaned | {"deterministic"}))

    def satisfies(self, required: Capabilities) -> bool:
        """Whether a worker with these can take a shard needing ``required``."""
        return required.names <= self.names

    def missing_for(self, required: Capabilities) -> frozenset[str]:
        return required.names - self.names

    def __or__(self, other: Capabilities) -> Capabilities:
        return Capabilities(self.names | other.names)

    def __bool__(self) -> bool:
        return bool(self.names)

    def render(self) -> str:
        return ", ".join(sorted(self.names))

    def to_list(self) -> list[str]:
        return sorted(self.names)


def capabilities_for(entity: CompiledEntity) -> Capabilities:
    """What generating this entity actually requires.

    Read off the compiled generators rather than declared by the user: a schema
    author should not have to remember that adding a ``tts`` field means only
    some of their workers can now produce that entity.
    """
    needed = {"deterministic"}
    for compiled in entity.fields:
        kind = type(compiled.generator).requires_provider
        if kind:
            needed.add(kind)
        elif compiled.generator.name == "document":
            # Documents need no provider but do write files, so a worker
            # without shared storage cannot usefully take them.
            needed.add("document")
    return Capabilities(frozenset(needed))


def describe_host() -> dict[str, Any]:
    """Enough about this machine for an operator to tell workers apart."""
    import os
    import platform
    import socket

    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count() or 1,
    }


@dataclass(slots=True)
class WorkerProfile:
    """A worker, as the controller knows it."""

    id: str
    capabilities: Capabilities
    #: How many shards this worker will hold at once.
    concurrency: int = 1
    host: dict[str, Any] = field(default_factory=dict)
    #: Which project it has. A worker holding a different schema would produce
    #: different records, so the controller checks rather than assumes.
    schema_hash: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "capabilities": self.capabilities.to_list(),
            "concurrency": self.concurrency,
            "host": dict(self.host),
            "schema_hash": self.schema_hash,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerProfile:
        return cls(
            id=str(data["id"]),
            capabilities=Capabilities.of(data.get("capabilities") or []),
            concurrency=max(1, int(data.get("concurrency", 1))),
            host=dict(data.get("host") or {}),
            schema_hash=str(data.get("schema_hash", "")),
            version=str(data.get("version", "")),
        )
