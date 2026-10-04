"""ArchOntos rule evaluation over an Ontology ``aec-facts-export/1`` (synthetic export, no DB)."""

import hashlib
import json

import pytest

from archontos.integration.ontology import (
    OntologyFactsError,
    evaluate_against_ontology,
    main,
    merge_context,
    rule_export_entry,
    rule_vars,
)

H = "ab" * 32


def parser_revision(doc_id, revision, source_hash):
    # Same identity Ontology uses (graphrag/integrations.parser_revision_id).
    raw = f"aec-parser-revision/1|{doc_id}|{revision}|{source_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


def export():
    return {
        "schema": "aec-facts-export/1",
        "producer": "khs0927/Ontology",
        "project_key": "주례동-315-4",
        "facts": {
            "building": {"floor_count": 5, "storeys": ["1F", "2F", "3F", "4F", "5F", "RF"]},
            "space": {"uses": ["계단실", "화장실"]},
        },
        "provenance": {
            "building.floor_count": {
                "method": "synthetic total fixture",
                "coverage": "complete",
                "authoritative_total": True,
                "kg_nodes": ["kg:st:x:5F"],
            },
        },
        "subjects": [
            {
                "kg_node": "kg:p:x",
                "type": "Project",
                "name": "주례동 315-4",
                "ref": {
                    "schema": "archontos-aec-subject-ref/1",
                    "project_id": "P-1",
                    "object_id": None,
                    "locator": {"kg_node": "kg:p:x"},
                },
            },
            {
                "kg_node": "kg:d:doc1",
                "type": "Drawing",
                "name": "A-101.dwg",
                "ref": {
                    "schema": "archontos-aec-subject-ref/1",
                    "project_id": "P-1",
                    "object_id": None,
                    "source_id": H,
                    "source_byte_revision_id": H,
                    "parser_revision_id": parser_revision("doc1", 0, H),
                    "locator": {"kg_node": "kg:d:doc1", "document_id": "doc1"},
                },
            },
        ],
    }


def stair_rule():
    return {
        "applicability": {
            "jurisdiction": ["KR-26"],
            "conditions": [{">=": [{"var": "building.floor_count"}, 5]}],
        },
        "rule": {
            "if": {">=": [{"var": "stair.direct_count"}, 2]},
            "then": {"PASS": {"reason": "ok"}},
            "else": {"FAIL": {"reason": "직통계단 부족"}},
        },
    }


def floors_rule():
    return {
        "rule": {
            "if": {"<=": [{"var": "building.floor_count"}, 5]},
            "then": {"PASS": {"reason": "5층 이하"}},
            "else": {"FAIL": {"reason": "5층 초과"}},
        }
    }


def test_absent_drawing_fact_goes_to_review_with_evidence():
    result = evaluate_against_ontology(
        stair_rule(), export(), context={"context": {"jurisdiction": "KR-26"}}, rule_id="R-1"
    )
    assert result["applicable"] is True and result["outcome"] == "REVIEW"
    assert result["missing_facts"] == ["stair.direct_count"]
    assert result["fact_evidence"]["building.floor_count"]["source"] == "ontology"
    assert result["fact_evidence"]["context.jurisdiction"] == {"source": "operator"}
    assert [r["project_id"] for r in result["applies_to"]] == ["P-1"]


def test_drawing_fact_decides_and_exports_link():
    result = evaluate_against_ontology(floors_rule(), export(), rule_id="R-2", version_label="v1")
    assert result["outcome"] == "PASS" and result["missing_facts"] == []
    entry = rule_export_entry(result, title="층수")
    assert entry["applies_to"][0]["schema"] == "archontos-aec-subject-ref/1"
    assert entry["outcome"] == "PASS" and entry["version_label"] == "v1"
    assert entry["binding"] is False and entry["canonical_context"] is None


@pytest.mark.parametrize(
    "provenance",
    [{"method": "highest nF"}, {"coverage": "partial"}, {"authoritative_total": False}],
)
def test_partial_floor_labels_cannot_certify_building_totals(provenance):
    partial = export()
    partial["provenance"]["building.floor_count"] = provenance
    result = evaluate_against_ontology(floors_rule(), partial)
    assert result["outcome"] == "REVIEW" and result["missing_facts"] == ["building.floor_count"]
    assert result["fact_warnings"]


def test_out_of_scope_jurisdiction_is_not_applicable():
    result = evaluate_against_ontology(
        stair_rule(), export(), context={"context": {"jurisdiction": "KR-11"}}
    )
    assert result["applicable"] is False and result["outcome"] is None


def test_operator_cannot_override_drawing_facts():
    merged, conflicts = merge_context(export()["facts"], {"building": {"floor_count": 3}})
    assert merged["building"]["floor_count"] == 5
    assert conflicts == [{"fact": "building.floor_count", "drawings": 5, "operator": 3}]
    result = evaluate_against_ontology(
        floors_rule(), export(), context={"building": {"floor_count": 3}}
    )
    assert result["outcome"] == "PASS" and result["context_conflicts"]


def test_invalid_export_and_subject_refs_are_rejected():
    with pytest.raises(OntologyFactsError):
        evaluate_against_ontology(floors_rule(), {"schema": "other/1", "facts": {}})
    bad = export()
    bad["subjects"][1]["ref"].pop("parser_revision_id")  # revision trio must come together
    with pytest.raises(OntologyFactsError, match="subjects\\[1\\]"):
        evaluate_against_ontology(floors_rule(), bad)
    with pytest.raises(OntologyFactsError, match="cannot be evaluated"):
        evaluate_against_ontology({"rule": {"if": {"xor": [1, 2]}, "then": {"PASS": {}}}}, export())


def test_rule_vars_include_scope_facts():
    assert rule_vars(stair_rule()) == [
        "building.floor_count",
        "stair.direct_count",
        "context.jurisdiction",
    ]


def test_cli_round_trip(tmp_path, capsys):
    (tmp_path / "rule.json").write_text(json.dumps(floors_rule()), encoding="utf-8")
    (tmp_path / "facts.json").write_text(json.dumps(export(), ensure_ascii=False), encoding="utf-8")
    links = tmp_path / "links.json"
    code = main(
        [
            "--rule",
            str(tmp_path / "rule.json"),
            "--facts",
            str(tmp_path / "facts.json"),
            "--rule-id",
            "R-2",
            "--title",
            "층수",
            "--export-links",
            str(links),
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "PASS"
    exported = json.loads(links.read_text(encoding="utf-8"))
    assert exported["schema"] == "archontos-rule-export/1"
    assert exported["rules"][0]["applies_to"][0]["project_id"] == "P-1"
    rule = str(tmp_path / "rule.json")
    assert main(["--rule", rule, "--facts", rule]) == 2
