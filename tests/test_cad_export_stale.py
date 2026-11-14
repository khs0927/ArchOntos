"""Stale edge export for Ontology (pure, file based)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archontos.cad.export_stale import (
    STALE_EDGES_SCHEMA,
    StaleExportError,
    build_stale_edges,
    dumps,
    main,
)
from archontos.domain.contracts import AecSubjectRef

H = "a" * 64
OBJ1 = "00000000-0000-0000-0000-000000000001"
OBJ2 = "00000000-0000-0000-0000-000000000002"
OBJ3 = "00000000-0000-0000-0000-000000000003"


def _ref(obj_id: str) -> dict:
    return AecSubjectRef(
        project_id="P-1",
        object_id=obj_id,
        source_id=H,
        source_byte_revision_id="b" * 64,
        parser_revision_id="c" * 64,
        locator={"kind": "space", "handles": ["1F"]},
    ).model_dump(mode="json")


def _results() -> list[dict]:
    return [
        {
            "id": "r-2",
            "run_id": "run-1",
            "rule_version_id": "rv-1",
            "rule_title": "복도 폭",
            "outcome": "PASS",
            "arch_object_ids": [OBJ2, OBJ1, OBJ3],
            "stale": True,
            "stale_by_diff_ids": ["d-2", "d-1"],
        },
        {
            "id": "r-1",
            "run_id": "run-1",
            "rule_version_id": "rv-2",
            "rule_title": "계단 폭",
            "outcome": "REVIEW",
            "arch_object_ids": [],
            "stale": True,
            "stale_by_diff_ids": ["d-1"],
        },
        {
            "id": "r-3",
            "run_id": "run-1",
            "rule_version_id": "rv-3",
            "outcome": "FAIL",
            "arch_object_ids": [OBJ1],
            "stale": False,
            "stale_by_diff_ids": [],
        },
    ]


def _objects() -> dict:
    return {
        "items": [
            {"id": OBJ1, "kind": "space", "subject_ref": _ref(OBJ1)},
            {"id": OBJ2, "kind": "space", "subject_ref": _ref(OBJ2)},
            {"id": OBJ3, "kind": "space", "subject_ref": None},
        ],
        "next_cursor": None,
    }


def test_only_stale_results_become_sorted_edges() -> None:
    doc = build_stale_edges({"items": _results()}, _objects(), revision_id="rev-1")
    assert doc["schema"] == STALE_EDGES_SCHEMA == "archontos-stale-edges/1"
    assert [e["result_id"] for e in doc["edges"]] == ["r-1", "r-2"]  # r-3 is fresh
    first, second = doc["edges"]
    assert first["applies_to"] == [] and first["unresolved_object_ids"] == []
    assert second["stale_by_diff_ids"] == ["d-1", "d-2"]
    assert [s["object_id"] for s in second["applies_to"]] == [OBJ1, OBJ2]
    assert second["unresolved_object_ids"] == [OBJ3]  # no stored subject ref: not invented
    assert all(e["stale"] and e["reason"] == "superseded_by_lineage_diff" for e in doc["edges"])
    assert doc["summary"] == {"edge_count": 2, "subject_count": 2, "unresolved_object_count": 1}
    for subject in second["applies_to"]:
        AecSubjectRef.model_validate(subject)  # same shape Ontology/ArchOntos share


def test_output_is_deterministic_and_hashed() -> None:
    a = build_stale_edges(_results(), _objects())
    b = build_stale_edges(list(reversed(_results())), _objects())
    assert dumps(a) == dumps(b)
    assert a["sha256"] == b["sha256"] and len(a["sha256"]) == 64
    assert build_stale_edges(_results(), _objects(), revision_id="x")["sha256"] != a["sha256"]
    assert build_stale_edges([], [])["edges"] == []


def test_bad_input_is_rejected() -> None:
    with pytest.raises(StaleExportError):
        build_stale_edges({"nope": 1}, [])
    with pytest.raises(StaleExportError):
        build_stale_edges([], ["x"])


def test_cli_writes_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results = tmp_path / "results.json"
    objects = tmp_path / "objects.json"
    out = tmp_path / "stale-edges.json"
    results.write_text(json.dumps({"items": _results()}), encoding="utf-8")
    objects.write_text(json.dumps(_objects()), encoding="utf-8")
    code = main(
        ["--results", str(results), "--objects", str(objects), "--revision-id", "rev-1"]
        + ["--out", str(out)]
    )
    assert code == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written == build_stale_edges(_results(), _objects(), revision_id="rev-1")
    assert out.read_text(encoding="utf-8").endswith("\n")
    assert main(["--results", str(tmp_path / "missing.json"), "--objects", str(objects)]) == 2
    assert "error:" in capsys.readouterr().err
