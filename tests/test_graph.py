"""Dependency graphs and ordering (design document sections 14, 15, 100, 101)."""

from __future__ import annotations

import pytest

from cacophony.core.errors import CircularDependencyError
from cacophony.schema.graph import DependencyGraph


def test_topological_order_respects_dependencies() -> None:
    graph = DependencyGraph.from_mapping(
        {
            "email": ["first_name", "last_name", "company"],
            "first_name": [],
            "last_name": [],
            "company": [],
        }
    )
    order = graph.topological_order()
    assert order.index("email") > order.index("first_name")
    assert order.index("email") > order.index("company")


def test_section_101_example() -> None:
    """The worked example from design document section 101."""
    graph = DependencyGraph.from_mapping(
        {
            "company": [],
            "first_name": [],
            "last_name": [],
            "age": [],
            "department": [],
            "title": [],
            "appearance_description": [],
            "email": ["first_name", "last_name", "company"],
            "biography": ["age", "title", "department"],
            "portrait_prompt": ["age", "appearance_description"],
            "portrait": ["portrait_prompt"],
        }
    )
    order = graph.topological_order()
    for node, dependencies in (
        ("email", ["first_name", "last_name", "company"]),
        ("biography", ["age", "title", "department"]),
        ("portrait_prompt", ["age", "appearance_description"]),
        ("portrait", ["portrait_prompt"]),
    ):
        for dependency in dependencies:
            assert order.index(dependency) < order.index(node)


def test_ordering_is_stable_against_declaration_order() -> None:
    """Independent nodes keep the order they were declared in."""
    graph = DependencyGraph.from_mapping({"c": [], "a": [], "b": []})
    assert graph.topological_order() == ["c", "a", "b"]


def test_repeated_ordering_is_identical() -> None:
    graph = DependencyGraph.from_mapping(
        {"d": ["a"], "c": ["a", "b"], "b": [], "a": [], "e": ["c", "d"]}
    )
    assert graph.topological_order() == graph.topological_order()


class TestCycles:
    def test_two_node_cycle_is_reported(self) -> None:
        graph = DependencyGraph.from_mapping({"a": ["b"], "b": ["a"]})
        with pytest.raises(CircularDependencyError) as exc:
            graph.topological_order()
        assert set(exc.value.cycle) == {"a", "b"}
        assert "->" in str(exc.value)

    def test_longer_cycle_is_reported(self) -> None:
        graph = DependencyGraph.from_mapping({"a": ["c"], "b": ["a"], "c": ["b"]})
        with pytest.raises(CircularDependencyError) as exc:
            graph.topological_order()
        assert set(exc.value.cycle) == {"a", "b", "c"}

    def test_self_dependency_is_rejected_immediately(self) -> None:
        graph = DependencyGraph()
        with pytest.raises(CircularDependencyError):
            graph.add_dependency("a", "a")

    def test_cycle_message_names_the_kind(self) -> None:
        graph = DependencyGraph.from_mapping({"x": ["y"], "y": ["x"]}, kind="entity")
        with pytest.raises(CircularDependencyError, match="Circular entity dependency"):
            graph.topological_order()

    def test_nodes_outside_the_cycle_are_not_blamed(self) -> None:
        graph = DependencyGraph.from_mapping({"ok": [], "a": ["b"], "b": ["a"]})
        with pytest.raises(CircularDependencyError) as exc:
            graph.topological_order()
        assert "ok" not in exc.value.cycle


class TestLayers:
    def test_layers_group_independent_nodes(self) -> None:
        graph = DependencyGraph.from_mapping({"a": [], "b": [], "c": ["a", "b"], "d": ["c"]})
        assert graph.layers() == [["a", "b"], ["c"], ["d"]]

    def test_layers_detect_cycles(self) -> None:
        graph = DependencyGraph.from_mapping({"a": ["b"], "b": ["a"]})
        with pytest.raises(CircularDependencyError):
            graph.layers()


def test_dependents_and_dependencies() -> None:
    graph = DependencyGraph.from_mapping({"a": [], "b": ["a"], "c": ["a"]})
    assert graph.dependencies_of("b") == {"a"}
    assert graph.dependents_of("a") == {"b", "c"}


def test_membership_and_length() -> None:
    graph = DependencyGraph.from_mapping({"a": [], "b": ["a"]})
    assert "a" in graph and "z" not in graph
    assert len(graph) == 2
