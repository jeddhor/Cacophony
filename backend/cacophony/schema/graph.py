"""Dependency graphs and topological ordering (design document sections 14, 15, 101).

Two graphs matter:

* the **field graph** inside one entity - ``email`` needs ``first_name`` and
  ``last_name`` before it can be built;
* the **entity graph** across the project - ``LoginEvent`` needs ``Employee``
  and ``Device`` to exist first.

Both are the same shape, so one implementation serves both. Ordering is a
deterministic topological sort: among nodes that are equally ready, the one
declared first in the schema wins. Without that tie-break the generation order
- and therefore, for anything reading sibling values, the output - could drift
between runs on the same schema.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ..core.errors import CircularDependencyError

__all__ = ["DependencyGraph"]


@dataclass(slots=True)
class DependencyGraph:
    """A directed graph of ``dependency -> dependent`` edges.

    Nodes keep their insertion order, which is the order they were declared in
    the schema.
    """

    kind: str = "field"
    _nodes: list[str] = field(default_factory=list)
    _node_set: set[str] = field(default_factory=set)
    _dependencies: dict[str, set[str]] = field(default_factory=dict)

    # -- construction ------------------------------------------------------- #

    def add_node(self, node: str) -> None:
        if node not in self._node_set:
            self._node_set.add(node)
            self._nodes.append(node)
            self._dependencies[node] = set()

    def add_dependency(self, node: str, depends_on: str) -> None:
        """Record that ``node`` cannot be produced until ``depends_on`` exists."""
        self.add_node(node)
        self.add_node(depends_on)
        if node != depends_on:
            self._dependencies[node].add(depends_on)
        else:
            raise CircularDependencyError([node], kind=self.kind)

    def add_dependencies(self, node: str, depends_on: Iterable[str]) -> None:
        for dependency in depends_on:
            self.add_dependency(node, dependency)

    @classmethod
    def from_mapping(
        cls, mapping: Mapping[str, Iterable[str]], kind: str = "field"
    ) -> DependencyGraph:
        graph = cls(kind=kind)
        for node in mapping:
            graph.add_node(node)
        for node, dependencies in mapping.items():
            graph.add_dependencies(node, dependencies)
        return graph

    # -- inspection --------------------------------------------------------- #

    @property
    def nodes(self) -> list[str]:
        return list(self._nodes)

    def dependencies_of(self, node: str) -> set[str]:
        return set(self._dependencies.get(node, ()))

    def dependents_of(self, node: str) -> set[str]:
        return {other for other, deps in self._dependencies.items() if node in deps}

    def __contains__(self, node: str) -> bool:
        return node in self._node_set

    def __len__(self) -> int:
        return len(self._nodes)

    # -- ordering ----------------------------------------------------------- #

    def topological_order(self) -> list[str]:
        """Return nodes in dependency order, stable against schema ordering.

        Raises :class:`CircularDependencyError` naming the actual cycle, which
        section 100 requires to be surfaced clearly.
        """
        position = {node: index for index, node in enumerate(self._nodes)}
        remaining = {node: set(deps) for node, deps in self._dependencies.items()}

        ready: list[str] = sorted(
            (node for node, deps in remaining.items() if not deps), key=position.__getitem__
        )
        ordered: list[str] = []

        while ready:
            node = ready.pop(0)
            ordered.append(node)
            del remaining[node]

            newly_ready = []
            for other, deps in remaining.items():
                if node in deps:
                    deps.discard(node)
                    if not deps:
                        newly_ready.append(other)
            if newly_ready:
                ready = sorted([*ready, *newly_ready], key=position.__getitem__)

        if remaining:
            raise CircularDependencyError(self._find_cycle(remaining), kind=self.kind)
        return ordered

    def layers(self) -> list[list[str]]:
        """Group nodes into levels that could be produced in parallel.

        Layer 0 depends on nothing; layer *n* depends only on layers < *n*.
        Used by the planner to describe achievable concurrency (section 30).
        """
        position = {node: index for index, node in enumerate(self._nodes)}
        remaining = {node: set(deps) for node, deps in self._dependencies.items()}
        result: list[list[str]] = []

        while remaining:
            layer = sorted(
                (node for node, deps in remaining.items() if not deps), key=position.__getitem__
            )
            if not layer:
                raise CircularDependencyError(self._find_cycle(remaining), kind=self.kind)
            result.append(layer)
            for node in layer:
                del remaining[node]
            for deps in remaining.values():
                deps.difference_update(layer)
        return result

    # -- cycle reporting ---------------------------------------------------- #

    def _find_cycle(self, remaining: Mapping[str, set[str]]) -> list[str]:
        """Recover one concrete cycle from the nodes that never became ready."""
        for start in remaining:
            trail = self._walk_to_cycle(start, remaining)
            if trail:
                return trail
        return sorted(remaining)

    @staticmethod
    def _walk_to_cycle(start: str, remaining: Mapping[str, set[str]]) -> list[str]:
        """Breadth-first search from ``start`` back to ``start``."""
        queue: deque[list[str]] = deque([[start]])
        seen: set[str] = set()
        while queue:
            path = queue.popleft()
            for dependency in sorted(remaining.get(path[-1], ())):
                if dependency == start:
                    # ``path`` runs dependent -> dependency; reverse it so the
                    # printed cycle reads in generation order.
                    return list(reversed(path))
                if dependency not in seen and dependency in remaining:
                    seen.add(dependency)
                    queue.append([*path, dependency])
        return []
