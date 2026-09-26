"""Synthetic RDF integrity cases; does not parse actual building files."""

import json
from pathlib import Path

from pyshacl import validate
from rdflib import OWL, RDF, Graph, Literal, Namespace

ROOT = Path(__file__).resolve().parent
A = Namespace("urn:arch:v4:")
P = Namespace("http://www.w3.org/ns/prov#")


def fixture():
    g = Graph()
    for node, cls in [
        (A.file1, A.SourceArtifact),
        (A.s1, A.ContentSnapshot),
        (A.run1, A.ExtractionRun),
        (A.e1, A.Evidence),
        (A.a1, A.RelationshipAssertion),
    ]:
        g.add((node, RDF.type, cls))
    for triple in [
        (A.s1, A.ofArtifact, A.file1),
        (A.s1, A.sha256, Literal("a" * 64)),
        (A.run1, A.inputSnapshot, A.s1),
        (A.run1, A.profileVersion, Literal("pdf-text/1")),
        (A.e1, A.fromSnapshot, A.s1),
        (A.e1, P.wasGeneratedBy, A.run1),
        (A.e1, A.locator, Literal('{"page":1,"text_span":[0,8]}')),
        (A.a1, A.subject, A.file1),
        (A.a1, A.predicate, A.correspondsTo),
        (A.a1, A.object, A.file2),
        (A.a1, A.evidence, A.e1),
        (A.a1, A.reviewState, Literal("PROVISIONAL")),
    ]:
        g.add(triple)
    return g


def main():
    shapes = Graph().parse(ROOT / "shapes.ttl")
    ontology = Graph().parse(ROOT / "ontology.ttl")
    cases = []

    def check(name, graph, expected):
        conforms, _, report = validate(graph, shacl_graph=shapes, ont_graph=ontology)
        passed = bool(conforms) == expected
        cases.append({"name": name, "passed": passed, "conforms": bool(conforms)})
        if not passed:
            raise AssertionError(report)

    check("valid_evidence_chain", fixture(), True)
    g = fixture()
    g.remove((A.e1, A.fromSnapshot, None))
    check("missing_snapshot_rejected", g, False)
    g = fixture()
    g.set((A.run1, A.inputSnapshot, A.s2))
    g.add((A.s2, RDF.type, A.ContentSnapshot))
    g.add((A.s2, A.ofArtifact, A.file1))
    g.add((A.s2, A.sha256, Literal("b" * 64)))
    check("cross_run_snapshot_rejected", g, False)
    g = fixture()
    g.set((A.a1, A.predicate, OWL.sameAs))
    check("file_sameas_rejected", g, False)
    g = fixture()
    g.set((A.a1, A.reviewState, Literal("ACCEPTED")))
    check("approval_without_reviewer_rejected", g, False)
    g = fixture()
    g.remove((A.s1, A.sha256, None))
    check("snapshot_without_bytes_digest_rejected", g, False)
    (ROOT / "ontology-validation.json").write_text(
        json.dumps({"kind": "synthetic_contract_tests", "cases": cases}, indent=2)
    )
    print(json.dumps({"ontology_tests": len(cases), "passed": sum(c["passed"] for c in cases)}))


if __name__ == "__main__":
    main()
