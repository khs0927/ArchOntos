from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class EvidenceDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    unchanged_count: int
    # Common keys where at least one side has no hash, because the normaliser
    # produced no text for it. Absence is not equality, so these are never
    # counted as unchanged; they are reported so a caller can tell "nothing
    # changed" from "we could not tell".
    indeterminate: tuple[str, ...]

    @property
    def completeness(self) -> Literal["complete", "partial"]:
        return "complete" if not self.indeterminate else "partial"


def diff_evidence_hashes(
    left: dict[str, str | None],
    right: dict[str, str | None],
) -> EvidenceDiff:
    left_keys = set(left)
    right_keys = set(right)
    common = left_keys & right_keys

    # A key hashed on only one side, or on neither, cannot be compared. Two
    # NULLs used to compare equal and were reported as unchanged, which let a
    # version whose text failed to extract entirely answer that no evidence
    # changed.
    indeterminate = sorted(key for key in common if left.get(key) is None or right.get(key) is None)
    comparable = [key for key in common if key not in set(indeterminate)]

    changed = sorted(key for key in comparable if left[key] != right[key])
    unchanged_count = len(comparable) - len(changed)

    return EvidenceDiff(
        added=tuple(sorted(right_keys - left_keys)),
        removed=tuple(sorted(left_keys - right_keys)),
        changed=tuple(changed),
        unchanged_count=unchanged_count,
        indeterminate=tuple(indeterminate),
    )
