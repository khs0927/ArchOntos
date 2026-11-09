"""Versioned mapping rules: raw CAD entities -> architectural object kinds.

A rule matches an entity by DXF type, layer-name regex and (optionally) block-name regex; the
first matching rule in list order wins, so block rules (more specific) come before layer rules.
The rule-set version is part of the parser revision, so changing a rule produces a new,
separately identified revision instead of silently rewriting old objects.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

OBJECT_KINDS = frozenset({"wall", "door", "window", "space", "stair", "column", "opening"})

_LINEAR = frozenset({"LINE", "LWPOLYLINE", "POLYLINE"})
_AREAL = frozenset({"LWPOLYLINE", "POLYLINE", "HATCH"})
_SYMBOL = frozenset({"INSERT"})


class MappingRuleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MappingRule:
    rule_id: str
    kind: str
    entity_types: frozenset[str]
    layer_pattern: str | None = None
    block_pattern: str | None = None
    #: Areal kinds (space) only accept closed polylines / hatches.
    require_closed: bool = False
    _layer_re: re.Pattern[str] | None = field(default=None, init=False, compare=False, repr=False)
    _block_re: re.Pattern[str] | None = field(default=None, init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in OBJECT_KINDS:
            raise MappingRuleError(f"rule {self.rule_id!r}: unknown kind {self.kind!r}")
        if not self.layer_pattern and not self.block_pattern:
            raise MappingRuleError(f"rule {self.rule_id!r}: needs a layer or block pattern")
        try:
            if self.layer_pattern:
                object.__setattr__(self, "_layer_re", re.compile(self.layer_pattern))
            if self.block_pattern:
                object.__setattr__(self, "_block_re", re.compile(self.block_pattern))
        except re.error as exc:
            raise MappingRuleError(f"rule {self.rule_id!r}: invalid regex: {exc}") from exc

    def matches(
        self, *, entity_type: str, layer: str, block_name: str | None, closed: bool
    ) -> bool:
        if entity_type not in self.entity_types:
            return False
        if self.require_closed and not closed:
            return False
        if self._layer_re is not None and not self._layer_re.search(layer):
            return False
        if self._block_re is not None:
            return block_name is not None and bool(self._block_re.search(block_name))
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "entity_types": sorted(self.entity_types),
            "layer_pattern": self.layer_pattern,
            "block_pattern": self.block_pattern,
            "require_closed": self.require_closed,
        }


@dataclass(frozen=True, slots=True)
class MappingRuleSet:
    version: str
    rules: tuple[MappingRule, ...]
    #: Layers whose TEXT/MTEXT names the space polygon that contains the text's insert point.
    space_label_pattern: str = r"(?i)^(A-AREA-IDEN|A-ROOM-NAME|ROOM-?NAME|실명|실이름)"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.version):
            raise MappingRuleError("rule-set version must match [A-Za-z0-9._-]{1,64}")
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise MappingRuleError("rule ids must be unique")
        re.compile(self.space_label_pattern)

    def match(
        self, *, entity_type: str, layer: str, block_name: str | None, closed: bool
    ) -> MappingRule | None:
        for rule in self.rules:
            if rule.matches(
                entity_type=entity_type, layer=layer, block_name=block_name, closed=closed
            ):
                return rule
        return None

    def is_space_label_layer(self, layer: str) -> bool:
        return re.search(self.space_label_pattern, layer) is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "space_label_pattern": self.space_label_pattern,
            "rules": [rule.as_dict() for rule in self.rules],
        }

    def fingerprint(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)


def rule_set_from_dict(data: dict[str, Any]) -> MappingRuleSet:
    """Build a rule set from its JSON form (``as_dict``); raises MappingRuleError."""
    try:
        rules = tuple(
            MappingRule(
                rule_id=str(item["rule_id"]),
                kind=str(item["kind"]),
                entity_types=frozenset(str(t).upper() for t in item["entity_types"]),
                layer_pattern=item.get("layer_pattern"),
                block_pattern=item.get("block_pattern"),
                require_closed=bool(item.get("require_closed", False)),
            )
            for item in data["rules"]
        )
        kwargs: dict[str, Any] = {"version": str(data["version"]), "rules": rules}
        if data.get("space_label_pattern"):
            kwargs["space_label_pattern"] = str(data["space_label_pattern"])
        return MappingRuleSet(**kwargs)
    except (KeyError, TypeError) as exc:
        raise MappingRuleError(f"malformed rule set: {exc}") from exc


# Korean office conventions (벽/문/창/계단/기둥/실) and the US National CAD Standard / AIA
# layer names (A-WALL, A-DOOR, A-GLAZ, A-FLOR-STRS, A-COLS, A-AREA). Patterns are anchored at
# the start so e.g. "문자" (text) or "A-WALL-PATT" hatching are handled deliberately below.
DEFAULT_RULE_SET = MappingRuleSet(
    version="kr-aia-2026.11",
    rules=(
        # Blocks first: a door block on a generic layer is still a door.
        MappingRule("door-block", "door", _SYMBOL, block_pattern=r"(?i)^(door|dr[-_]?\d|문|도어)"),
        MappingRule(
            "window-block", "window", _SYMBOL, block_pattern=r"(?i)^(window|win[-_]?\d|창(?!고))"
        ),
        MappingRule(
            "door-layer", "door", _SYMBOL, layer_pattern=r"(?i)^(A-DOOR|DOOR|문(?!자)|도어)"
        ),
        MappingRule(
            "window-layer", "window", _SYMBOL, layer_pattern=r"(?i)^(A-GLAZ|A-WIND|WINDOW|창)"
        ),
        MappingRule(
            "opening-layer", "opening", _SYMBOL, layer_pattern=r"(?i)^(A-OPEN|OPENING|개구부)"
        ),
        MappingRule(
            "column", "column", _SYMBOL | _AREAL, layer_pattern=r"(?i)^(A-COLS|S-COLS|COLUMN|기둥)"
        ),
        MappingRule(
            "stair",
            "stair",
            _SYMBOL | _AREAL,
            layer_pattern=r"(?i)^(A-FLOR-STRS|A-STRS|STAIR|계단)",
        ),
        MappingRule(
            "space",
            "space",
            _AREAL,
            layer_pattern=r"(?i)^(A-AREA(?!-IDEN)|A-ROOM(?!-NAME)|A-SPACE|ROOM(?!-?NAME)|SPACE|실(?!명|이름)|공간|면적)",
            require_closed=True,
        ),
        MappingRule(
            "wall",
            "wall",
            _LINEAR,
            layer_pattern=r"(?i)^(A-WALL(?!-PATT)|WALL|벽)",
        ),
    ),
)
