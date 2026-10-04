"""Operator context cannot replace a canonical fact's scalar prefix or hide conflicts."""

import pytest

from archontos.integration.ontology import OntologyFactsError, merge_context


@pytest.mark.parametrize("scalar", [5, False, None, "canonical", ["a", "b"]])
def test_scalar_prefix_is_preserved(scalar):
    original = {"building": {"height": scalar}}
    merged, conflicts = merge_context(original, {"building": {"height": {"value": 3}}})
    assert merged == original and len(conflicts) == 1
    assert conflicts[0]["blocked_by"] == "building.height"


def test_nested_dotted_prefix_cannot_change_rule_branch():
    facts = {"building": 5}
    merged, conflicts = merge_context(facts, {"building.floor_count": 100})
    assert merged == facts
    assert conflicts == [
        {"fact": "building.floor_count", "drawings": 5, "operator": 100, "blocked_by": "building"}
    ]


def test_context_can_add_siblings_and_empty_context_is_noop():
    facts = {"building": {"height": 5}}
    assert merge_context(facts, {}) == (facts, [])
    merged, conflicts = merge_context(facts, {"building": {"use_group": "synthetic"}})
    assert merged == {"building": {"height": 5, "use_group": "synthetic"}} and not conflicts
    assert facts == {"building": {"height": 5}}


def test_ambiguous_operator_paths_fail_closed():
    with pytest.raises(OntologyFactsError, match="conflicting operator"):
        merge_context({}, {"building": {"height": 5}, "building.height": 6})
    with pytest.raises(OntologyFactsError, match="empty components"):
        merge_context({}, {"building..height": 5})
    with pytest.raises(OntologyFactsError, match="conflicting operator fact prefixes"):
        merge_context({}, {"building": 5, "building.height": 6})


def test_operator_unit_paths_are_atomic_and_canonical_units_preserved():
    merged, conflicts = merge_context(
        {"building": {"height": 2}}, {"fact_units": {"building.height": "m"}}
    )
    assert merged["fact_units"] == {"building.height": "m"} and not conflicts
    kept, conflicts = merge_context(merged, {"fact_units": {"building.height": "mm"}})
    assert kept == merged and conflicts


def test_unit_declarations_use_a_single_unambiguous_shape():
    with pytest.raises(OntologyFactsError, match="fact_units object"):
        merge_context({}, {"fact_units.building.height": "m"})
