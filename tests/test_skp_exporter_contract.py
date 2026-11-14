"""P2: the SketchUp Ruby exporter's output shape (Ruby itself cannot run in CI).

``tools/sketchup/sample_export.json`` is what ``tools/sketchup/archontos_export.rb`` writes for
the documented fixture model (a 6 x 4 m room on 1F: wall group, classified door, window, room
group with a floor face, an unclassified chair; storeys 1F/2F in the model attributes). The test
checks it against the contract schema, parses it, and checks the Ruby source emits exactly the
contract's keys.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from archontos.cad.skp import SKP_EXPORT_SCHEMA, SKP_SCHEMA_ID, parse_skp_json, validate_export

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "sketchup"


def test_sample_export_validates_and_parses() -> None:
    data = (TOOLS / "sample_export.json").read_bytes()
    document = json.loads(data)
    validate_export(document)
    drawing = parse_skp_json(data)
    kinds = sorted(o.kind for o in drawing.objects)
    assert kinds == ["door", "space", "wall", "window"]
    assert drawing.unmapped_entity_count == 1  # the chair
    door = next(o for o in drawing.objects if o.kind == "door")
    assert ("ifc_guid", "2O2Fr$t4X7Zf8NOew3FLOH") in door.source_refs
    space = next(o for o in drawing.objects if o.kind == "space")
    assert space.properties["area"] == {"value": 24.0, "unit": "m2"}
    assert [s["name"] for s in drawing.model_meta["storeys"]] == ["1F", "2F"]


def test_ruby_exporter_emits_the_contract_keys() -> None:
    source = (TOOLS / "archontos_export.rb").read_text(encoding="utf-8")
    assert f"SCHEMA = '{SKP_SCHEMA_ID}'" in source
    emitted = set(re.findall(r"'([a-z_]+)' =>", source))
    emitted |= set(re.findall(r"(?:out|doc)\['([a-z_]+)'\] =", source))
    entity_keys = set(SKP_EXPORT_SCHEMA["properties"]["entities"]["items"]["properties"])
    model_keys = set(SKP_EXPORT_SCHEMA["properties"]["model"]["properties"])
    top_keys = set(SKP_EXPORT_SCHEMA["properties"])
    storey_keys = set(SKP_EXPORT_SCHEMA["properties"]["storeys"]["items"]["properties"])
    allowed = entity_keys | model_keys | top_keys | storey_keys | {"min", "max"}
    assert emitted <= allowed, emitted - allowed
    required = set(SKP_EXPORT_SCHEMA["properties"]["entities"]["items"]["required"])
    assert required <= emitted
    # every key the exporter writes for an entity appears in the sample (shape parity)
    sample = json.loads((TOOLS / "sample_export.json").read_text(encoding="utf-8"))
    sample_keys = {k for e in sample["entities"] for k in e}
    assert (emitted & entity_keys) == sample_keys
