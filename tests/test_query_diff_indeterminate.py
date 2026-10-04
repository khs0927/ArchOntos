"""A diff over evidence may not answer from a hash that was never produced.

The normaliser returns no text hash when it extracted no text, so a row can
carry a NULL. Two NULLs used to compare equal and were counted as unchanged,
which let a version whose text failed to extract entirely answer that no
evidence changed. These tests pin that distinction.
"""

from archontos.query.diff import diff_evidence_hashes

H1 = "a" * 64
H2 = "b" * 64


def test_two_nulls_are_not_unchanged_evidence():
    """The exact failure: extraction produced nothing on either side."""
    diff = diff_evidence_hashes({"k1": None}, {"k1": None})

    assert diff.unchanged_count == 0
    assert diff.changed == ()
    assert diff.indeterminate == ("k1",)
    assert diff.completeness == "partial"


def test_a_null_on_either_side_is_indeterminate():
    left = {"kept": H1, "lost": H1, "gained": None}
    right = {"kept": H1, "lost": H2, "gained": H2}

    diff = diff_evidence_hashes(left, right)

    assert diff.unchanged_count == 1
    assert diff.changed == ("lost",)
    assert diff.indeterminate == ("gained",)
    assert diff.completeness == "partial"


def test_fully_hashed_comparison_is_complete():
    diff = diff_evidence_hashes({"same": H1, "differs": H1}, {"same": H1, "differs": H2})

    assert diff.unchanged_count == 1
    assert diff.changed == ("differs",)
    assert diff.indeterminate == ()
    assert diff.completeness == "complete"


def test_added_and_removed_are_still_reported():
    diff = diff_evidence_hashes({"only_left": H1}, {"only_right": H1})

    assert diff.added == ("only_right",)
    assert diff.removed == ("only_left",)
    assert diff.unchanged_count == 0
    assert diff.indeterminate == ()


def test_unchanged_count_excludes_indeterminate_keys():
    """The count must not be inflated by keys we could not compare."""
    diff = diff_evidence_hashes(
        {"a": H1, "b": H1, "c": H1, "d": H1},
        {"a": H1, "b": H1, "c": None, "d": None},
    )

    assert diff.unchanged_count == 2
    assert diff.indeterminate == ("c", "d")


def test_mixed_null_and_hashed_never_reports_a_false_unchanged():
    """A regression guard on the original comparison."""
    diff = diff_evidence_hashes({"a": None, "b": H1, "c": H1}, {"a": None, "b": H1, "c": H2})

    # 'a' must not appear anywhere as a decided verdict.
    assert "a" not in diff.changed
    assert diff.unchanged_count == 1
    assert diff.indeterminate == ("a",)
    assert diff.completeness == "partial"
