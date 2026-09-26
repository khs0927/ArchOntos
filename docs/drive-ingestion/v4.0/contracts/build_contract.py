import json
from pathlib import Path

p = Path(__file__).parent
schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ExtractionResult v4 reference contract",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contract_version",
        "job_id",
        "snapshot_id",
        "status",
        "extractor",
        "coverage",
        "evidence",
        "assertions",
        "issues",
    ],
    "properties": {
        "contract_version": {"const": "4.0"},
        "job_id": {"type": "string", "minLength": 1},
        "snapshot_id": {"type": "string", "minLength": 1},
        "status": {
            "enum": [
                "COMPLETE",
                "PARTIAL",
                "UNSUPPORTED",
                "INACCESSIBLE",
                "ERROR_RETRYABLE",
                "ERROR_FINAL",
                "REVIEW_REQUIRED",
            ]
        },
        "extractor": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "version", "config_hash"],
            "properties": {
                x: {"type": "string", "minLength": 1} for x in ["name", "version", "config_hash"]
            },
        },
        "coverage": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "scope",
                "profile_id",
                "profile_version",
                "expected",
                "processed",
                "unknown_remainder",
            ],
            "properties": {
                "profile_id": {"type": "string", "minLength": 1},
                "profile_version": {"type": "string", "minLength": 1},
                "scope": {"enum": ["METADATA", "TEXT", "GEOMETRY", "TABLES"]},
                "expected": {"type": ["integer", "null"], "minimum": 0},
                "processed": {"type": "integer", "minimum": 0},
                "unknown_remainder": {"type": "boolean"},
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["evidence_id", "locator"],
                "properties": {
                    "evidence_id": {"type": "string", "minLength": 1},
                    "locator": {"type": "object", "minProperties": 1},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "assertions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject", "predicate", "object", "evidence_id", "review_state"],
                "properties": {
                    "subject": {"type": "string", "minLength": 1},
                    "predicate": {
                        "type": "string",
                        "minLength": 1,
                        "not": {"enum": ["owl:sameAs", "http://www.w3.org/2002/07/owl#sameAs"]},
                    },
                    "object": {},
                    "evidence_id": {"type": "string", "minLength": 1},
                    "review_state": {"const": "PROVISIONAL"},
                },
            },
        },
        "issues": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
    "allOf": [
        {
            "if": {"properties": {"status": {"const": "COMPLETE"}}},
            "then": {
                "properties": {
                    "coverage": {
                        "properties": {
                            "unknown_remainder": {"const": False},
                            "expected": {"type": "integer"},
                        }
                    },
                    "issues": {"maxItems": 0},
                }
            },
        },
        {
            "if": {"properties": {"status": {"not": {"const": "COMPLETE"}}}},
            "then": {"properties": {"issues": {"minItems": 1}}},
        },
        {
            "if": {
                "properties": {
                    "status": {
                        "enum": ["UNSUPPORTED", "INACCESSIBLE", "ERROR_RETRYABLE", "ERROR_FINAL"]
                    }
                }
            },
            "then": {"properties": {"assertions": {"maxItems": 0}}},
        },
    ],
}
(p / "extraction-result.schema.json").write_text(
    json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
)
