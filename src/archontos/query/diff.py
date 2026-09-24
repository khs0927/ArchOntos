from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvidenceDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    unchanged_count: int


def diff_evidence_hashes(
    left: dict[str, str | None],
    right: dict[str, str | None],
) -> EvidenceDiff:
    left_keys = set(left)
    right_keys = set(right)
    common = left_keys & right_keys

    changed = sorted(key for key in common if left.get(key) != right.get(key))
    unchanged_count = len(common) - len(changed)

    return EvidenceDiff(
        added=tuple(sorted(right_keys - left_keys)),
        removed=tuple(sorted(left_keys - right_keys)),
        changed=tuple(changed),
        unchanged_count=unchanged_count,
    )
