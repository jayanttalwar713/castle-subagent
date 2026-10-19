"""Dependency graph: the only place the system serializes."""

from chatsched.depgraph import DependencyGraph


def test_edge_records_parent_and_child():
    g = DependencyGraph()
    assert g.add_edge("b", "a") is True
    assert g.parent_of("b") == "a"
    assert g.children_of("a") == {"b"}


def test_self_edge_is_a_cycle():
    g = DependencyGraph()
    assert g.add_edge("a", "a") is False
    assert g.parent_of("a") is None


def test_direct_cycle_is_refused():
    g = DependencyGraph()
    g.add_edge("b", "a")
    # Only classifier error can produce this; we detect rather than trust.
    assert g.add_edge("a", "b") is False
    assert g.parent_of("a") is None


def test_transitive_cycle_is_refused():
    g = DependencyGraph()
    g.add_edge("b", "a")
    g.add_edge("c", "b")
    assert g.add_edge("a", "c") is False


def test_release_detaches_children():
    g = DependencyGraph()
    g.add_edge("b", "a")
    g.add_edge("c", "a")
    assert g.release("a") == {"b", "c"}
    # Released children are no longer waiting on anything.
    assert g.parent_of("b") is None
    assert g.children_of("a") == set()


def test_release_is_idempotent():
    g = DependencyGraph()
    g.add_edge("b", "a")
    g.release("a")
    assert g.release("a") == set()


def test_repointing_an_edge_drops_the_old_one():
    g = DependencyGraph()
    g.add_edge("c", "a")
    g.add_edge("c", "b")
    assert g.parent_of("c") == "b"
    assert g.children_of("a") == set()
