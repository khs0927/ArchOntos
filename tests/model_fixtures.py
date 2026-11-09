"""MVP-4 fixtures: SketchUp JSON exports (pure) and a tiny IFC built with ifcopenshell."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

#: GlobalId shared by the IFC door and the SketchUp door (cross-source link).
DOOR_GUID = "2O2Fr$t4X7Zf8NOew3FLOH"
WALL_GUID = "1hqIFTRjfV6AWq_bMtnZwI"


def skp_export(*, door_width_mm: float = 900.0, extra: list[dict] | None = None) -> dict:
    """6 x 4 m room: door (IFC GUID), wall, space with footprint, stair flight, a chair."""
    entities: list[dict[str, Any]] = [
        {
            "persistent_id": 101,
            "entity_type": "ComponentInstance",
            "tag": "문",
            "definition": "Door 900",
            "ifc_guid": DOOR_GUID,
            "storey": "1F",
            "bbox": {"min": [500, 0, 0], "max": [500 + door_width_mm, 50, 2100]},
            "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 500, 0, 0, 1],
        },
        {
            "persistent_id": 102,
            "entity_type": "Group",
            "tag": "A-WALL",
            "bbox": {"min": [0, 0, 0], "max": [6000, 200, 2700]},
        },
        {
            "persistent_id": 103,
            "entity_type": "Group",
            "tag": "실",
            "name": "거실",
            "storey": "1F",
            "footprint": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]],
            "bbox": {"min": [0, 0, 0], "max": [6000, 4000, 2700]},
        },
        {
            "persistent_id": 104,
            "entity_type": "ComponentInstance",
            "tag": "Untagged",
            "definition": "Chair",
            "bbox": {"min": [1000, 1000, 0], "max": [1500, 1500, 900]},
        },
        {
            "persistent_id": "s-105",
            "entity_type": "Group",
            "tag": "Untagged",
            "ifc_type": "IfcStairFlight",
            "bbox": {"min": [4000, 1000, 0], "max": [5200, 4000, 3000]},
            "quantities": {"width": {"value": 1200, "unit": "mm"}},
        },
    ]
    return {
        "schema": "archontos-skp-export/1",
        "model": {"name": "test", "units": "mm", "exporter": "pytest"},
        "storeys": [{"name": "1F", "elevation": 0}],
        "entities": entities + (extra or []),
    }


def skp_bytes(**kwargs: Any) -> bytes:
    return json.dumps(skp_export(**kwargs), ensure_ascii=False).encode("utf-8")


def tiny_ifc(*, door_width_mm: float = 900.0) -> bytes:
    """IFC4 in millimetres: storeys B1/1F/2F, door, wall, space (Qto + extruded profile),
    stair with one flight, slab, and an unmapped beam."""
    pytest.importorskip("ifcopenshell")
    import ifcopenshell
    import ifcopenshell.guid

    f = ifcopenshell.file(schema="IFC4")

    def gid(name: str) -> str:
        return ifcopenshell.guid.compress(uuid.uuid5(uuid.NAMESPACE_URL, name).hex)

    def e(cls: str, **attrs: Any) -> Any:
        return f.create_entity(cls, **attrs)

    def point(*coords: float) -> Any:
        return e("IfcCartesianPoint", Coordinates=tuple(float(c) for c in coords))

    def placement(x: float, y: float, z: float, rel: Any = None) -> Any:
        return e(
            "IfcLocalPlacement",
            PlacementRelTo=rel,
            RelativePlacement=e("IfcAxis2Placement3D", Location=point(x, y, z)),
        )

    ctx = e(
        "IfcGeometricRepresentationContext",
        ContextType="Model",
        CoordinateSpaceDimension=3,
        Precision=1e-5,
        WorldCoordinateSystem=e("IfcAxis2Placement3D", Location=point(0, 0, 0)),
    )
    units = e(
        "IfcUnitAssignment",
        Units=[
            e("IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE"),
            e("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE"),
            e("IfcSIUnit", UnitType="VOLUMEUNIT", Name="CUBIC_METRE"),
        ],
    )
    project = e(
        "IfcProject",
        GlobalId=gid("project"),
        Name="P",
        RepresentationContexts=[ctx],
        UnitsInContext=units,
    )
    site_pl = placement(0, 0, 0)
    site = e("IfcSite", GlobalId=gid("site"), Name="Site", ObjectPlacement=site_pl)
    bldg_pl = placement(0, 0, 0, site_pl)
    building = e("IfcBuilding", GlobalId=gid("bldg"), Name="B", ObjectPlacement=bldg_pl)
    storeys = {}
    for name, elevation in (("B1", -3000.0), ("1F", 0.0), ("2F", 3000.0)):
        storeys[name] = e(
            "IfcBuildingStorey",
            GlobalId=gid(f"storey-{name}"),
            Name=name,
            Elevation=elevation,
            ObjectPlacement=placement(0, 0, elevation, bldg_pl),
        )
    e("IfcRelAggregates", GlobalId=gid("r1"), RelatingObject=project, RelatedObjects=[site])
    e("IfcRelAggregates", GlobalId=gid("r2"), RelatingObject=site, RelatedObjects=[building])
    e(
        "IfcRelAggregates",
        GlobalId=gid("r3"),
        RelatingObject=building,
        RelatedObjects=list(storeys.values()),
    )
    first = storeys["1F"]
    first_pl = first.ObjectPlacement

    def qto(owner: Any, name: str, quantities: list[Any]) -> None:
        q = e(
            "IfcElementQuantity",
            GlobalId=gid(f"q-{name}-{owner.Name}"),
            Name=name,
            Quantities=quantities,
        )
        e(
            "IfcRelDefinesByProperties",
            GlobalId=gid(f"rq-{name}-{owner.Name}"),
            RelatedObjects=[owner],
            RelatingPropertyDefinition=q,
        )

    def length(name: str, value: float) -> Any:
        return e("IfcQuantityLength", Name=name, LengthValue=value)

    def area(name: str, value: float) -> Any:
        return e("IfcQuantityArea", Name=name, AreaValue=value)

    door = e(
        "IfcDoor",
        GlobalId=DOOR_GUID,
        Name="D1",
        ObjectPlacement=placement(500, 0, 0, first_pl),
        OverallWidth=door_width_mm,
        OverallHeight=2100.0,
    )
    wall = e("IfcWall", GlobalId=WALL_GUID, Name="W1", ObjectPlacement=placement(0, 0, 0, first_pl))
    qto(
        wall,
        "Qto_WallBaseQuantities",
        [length("Length", 6000.0), length("Width", 200.0), length("Height", 2700.0)],
    )
    slab = e(
        "IfcSlab", GlobalId=gid("slab"), Name="S1", ObjectPlacement=placement(0, 0, 0, first_pl)
    )
    qto(slab, "Qto_SlabBaseQuantities", [area("NetArea", 24.0), length("Width", 200.0)])
    beam = e(
        "IfcBeam", GlobalId=gid("beam"), Name="B1", ObjectPlacement=placement(0, 0, 2700, first_pl)
    )
    stair = e(
        "IfcStair",
        GlobalId=gid("stair"),
        Name="ST1",
        ObjectPlacement=placement(4000, 0, 0, first_pl),
    )
    flight = e(
        "IfcStairFlight",
        GlobalId=gid("flight"),
        Name="F1",
        ObjectPlacement=placement(4000, 1000, 0, first_pl),
    )
    e("IfcRelAggregates", GlobalId=gid("r4"), RelatingObject=stair, RelatedObjects=[flight])
    pset = e(
        "IfcPropertySet",
        GlobalId=gid("pset-flight"),
        Name="Pset_StairFlightCommon",
        HasProperties=[
            e(
                "IfcPropertySingleValue",
                Name="Width",
                NominalValue=f.create_entity("IfcPositiveLengthMeasure", 1200.0),
            )
        ],
    )
    e(
        "IfcRelDefinesByProperties",
        GlobalId=gid("rp-flight"),
        RelatedObjects=[flight],
        RelatingPropertyDefinition=pset,
    )
    e(
        "IfcRelContainedInSpatialStructure",
        GlobalId=gid("r5"),
        RelatingStructure=first,
        RelatedElements=[door, wall, slab, beam, stair],
    )
    profile = e(
        "IfcRectangleProfileDef",
        ProfileType="AREA",
        XDim=6000.0,
        YDim=4000.0,
        Position=e("IfcAxis2Placement2D", Location=point(3000, 2000)),
    )
    solid = e(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=e("IfcAxis2Placement3D", Location=point(0, 0, 0)),
        ExtrudedDirection=e("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=2700.0,
    )
    shape = e(
        "IfcProductDefinitionShape",
        Representations=[
            e(
                "IfcShapeRepresentation",
                ContextOfItems=ctx,
                RepresentationIdentifier="Body",
                RepresentationType="SweptSolid",
                Items=[solid],
            )
        ],
    )
    space = e(
        "IfcSpace",
        GlobalId=gid("space"),
        Name="101",
        LongName="거실",
        ObjectPlacement=placement(0, 0, 0, first_pl),
        Representation=shape,
    )
    e("IfcRelAggregates", GlobalId=gid("r6"), RelatingObject=first, RelatedObjects=[space])
    qto(space, "Qto_SpaceBaseQuantities", [area("NetFloorArea", 24.0), length("Height", 2700.0)])
    return f.to_string().encode("utf-8")
