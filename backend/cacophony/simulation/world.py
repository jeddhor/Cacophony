"""Persistent synthetic worlds (design document sections 16, 73, 93).

    "Acme Test Corporation": 4,800 employees, 27 offices, 8,000 endpoints,
    1,200 servers, 37 applications, 14 months of telemetry.

    Once created, additional datasets can be generated against the same world.

The point of a world is that the *people persist*. Generate authentication logs
today and helpdesk tickets next week, and employee E48291 must be the same
person in both - same name, same department, same manager, same laptop - or the
two datasets cannot be joined and the exercise was pointless.

Almost all of that already exists. Because a record is derived by hashing its
position (section 75), a schema and a seed together *are* a world: run them
twice and the same five thousand people come out. Nothing has to be stored for
that to be true.

So a world here is deliberately thin: a name, the seed, the schema revision it
was created from, and the sizes of its populations. Storing more would be
storing a copy of something that can be recomputed exactly, which section 42
warns against - generated data does not belong in the metadata database.

What the record *does* buy is the three things recomputation cannot give you:

**A name.** ``--world acme`` beats remembering that the seed was 90210.

**A guarantee.** A world pins its schema revision, so generating against it a
month later cannot silently produce different people because someone edited an
entity in between. If the schema has moved on, that is a conflict a person
should see rather than a difference they discover in a join.

**A record of what has been drawn from it.** Which runs used this world, and
what they produced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from ..schema.plan import CompiledProject

__all__ = ["World", "WorldStore"]


@dataclass(slots=True)
class World:
    """A named, reproducible population."""

    name: str
    seed: int
    #: The schema this world was created from, by content hash. Two worlds with
    #: the same hash contain the same people.
    schema_hash: str = ""
    #: Entity name to population size. What "4,800 employees" means.
    populations: dict[str, int] = field(default_factory=dict)
    description: str | None = None
    created_at: str = ""
    #: Run ids that have drawn from this world.
    runs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "seed": self.seed,
            "schema_hash": self.schema_hash,
            "populations": dict(self.populations),
            "description": self.description,
            "created_at": self.created_at,
            "runs": list(self.runs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> World:
        return cls(
            name=str(data["name"]),
            seed=int(data["seed"]),
            schema_hash=str(data.get("schema_hash", "")),
            populations={str(k): int(v) for k, v in (data.get("populations") or {}).items()},
            description=data.get("description"),
            created_at=str(data.get("created_at", "")),
            runs=list(data.get("runs") or []),
        )

    @classmethod
    def of(cls, name: str, compiled: CompiledProject, *, description: str | None = None) -> World:
        """Capture the world a compiled project describes."""
        return cls(
            name=name,
            seed=compiled.seed,
            schema_hash=schema_hash(compiled),
            populations={entity.name: entity.count for entity in compiled.ordered_entities()},
            description=description or compiled.spec.project.description,
            created_at=datetime.now().replace(microsecond=0).isoformat(),
        )

    # -- consistency ---------------------------------------------------------- #

    def conflicts_with(self, compiled: CompiledProject) -> list[str]:
        """Why this project would not produce this world's people.

        Reported rather than enforced: a user who has genuinely changed the
        schema should be told what moved, not blocked. Silently generating
        different people under the same world name is the only outcome that is
        never acceptable.
        """
        problems: list[str] = []
        if compiled.seed != self.seed:
            problems.append(
                f"the project's seed is {compiled.seed}, but world '{self.name}' was "
                f"created with {self.seed}; its people would all be different"
            )

        current = schema_hash(compiled)
        if self.schema_hash and current != self.schema_hash:
            changed = [
                name
                for name, size in self.populations.items()
                if name in compiled.entities and compiled.entity(name).count != size
            ]
            missing = [name for name in self.populations if name not in compiled.entities]
            detail = ""
            if changed:
                detail = "; population changed for " + ", ".join(sorted(changed))
            if missing:
                detail += "; missing " + ", ".join(sorted(missing))
            problems.append(f"the schema has changed since this world was created{detail}")
        return problems

    def apply_to(self, compiled: CompiledProject) -> None:
        """Make a project generate *this* world's people.

        Only the seed is imposed. Populations are not: generating a second,
        smaller dataset against the same world is the normal case, and the
        first N people of a population are the same people whether N is a
        hundred or five thousand.
        """
        compiled.spec.project.seed = self.seed


class WorldStore:
    """Worlds on disk, beside the project.

    A JSON file rather than a table in the run store: a world is part of what a
    team shares and reviews (section 74), not a private artefact of one
    machine's run history.
    """

    FILENAME = "worlds.json"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.is_dir():
            self.path = self.path / self.FILENAME

    # -- reading -------------------------------------------------------------- #

    def load(self) -> dict[str, World]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SchemaError(f"could not read the world file {self.path}: {exc}") from exc

        worlds = payload.get("worlds") if isinstance(payload, dict) else payload
        return {str(entry["name"]): World.from_dict(entry) for entry in (worlds or [])}

    def get(self, name: str) -> World | None:
        return self.load().get(name)

    def names(self) -> list[str]:
        return sorted(self.load())

    def __iter__(self) -> Iterator[World]:
        return iter(self.load().values())

    # -- writing -------------------------------------------------------------- #

    def save(self, world: World) -> World:
        worlds = self.load()
        existing = worlds.get(world.name)
        if existing is not None:
            # Keep the history: a world that has been drawn from twice should
            # say so, and its creation date is the one that matters.
            world.runs = list(dict.fromkeys([*existing.runs, *world.runs]))
            world.created_at = existing.created_at or world.created_at
        worlds[world.name] = world
        self._write(worlds)
        return world

    def record_run(self, name: str, run_id: str) -> None:
        worlds = self.load()
        world = worlds.get(name)
        if world is None:
            return
        if run_id not in world.runs:
            world.runs.append(run_id)
            self._write(worlds)

    def delete(self, name: str) -> bool:
        worlds = self.load()
        if worlds.pop(name, None) is None:
            return False
        self._write(worlds)
        return True

    def _write(self, worlds: dict[str, World]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {"worlds": [world.to_dict() for world in worlds.values()]},
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise SchemaError(f"could not write the world file {self.path}: {exc}") from exc


def schema_hash(compiled: CompiledProject) -> str:
    """A content hash of everything that decides who the people are.

    Deliberately not a hash of the whole file: comments, output profiles and
    provider URLs can all change without changing a single generated value, and
    a world that reported a conflict every time someone edited a comment would
    be a world nobody used.
    """
    import hashlib

    material: list[str] = []
    for entity in compiled.ordered_entities():
        for compiled_field in entity.fields:
            spec = compiled_field.spec
            material.append(
                f"{entity.name}.{compiled_field.name}:{spec.type.value}:"
                f"{compiled_field.generator_name}:"
                f"{sorted(compiled_field.generator.options.items(), key=str)}"
            )
    return hashlib.blake2b("\n".join(material).encode("utf-8"), digest_size=16).hexdigest()
