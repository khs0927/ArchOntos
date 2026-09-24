from archontos.query.diff import diff_evidence_hashes


def test_evidence_diff_detects_added_removed_changed_and_unchanged():
    result = diff_evidence_hashes(
        {
            "article:1": "aaa",
            "article:2": "bbb",
            "article:3": "ccc",
        },
        {
            "article:1": "aaa",
            "article:2": "changed",
            "article:4": "ddd",
        },
    )
    assert result.added == ("article:4",)
    assert result.removed == ("article:3",)
    assert result.changed == ("article:2",)
    assert result.unchanged_count == 1
