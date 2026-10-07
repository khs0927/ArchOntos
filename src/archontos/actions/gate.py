"""Approval gate for write actions.

Canonical rows live in ``action`` / ``action_run``. A write stays a proposal
until this gate says it may run. Projection rebuilds are not write actions.
"""

from __future__ import annotations

TERMINAL = frozenset({"succeeded", "failed", "rejected"})


class ApprovalDenied(Exception):
    def __init__(self, status: str, requires_approval: bool):
        self.status = status
        self.requires_approval = requires_approval
        super().__init__(
            f"action cannot run from status={status!r} requires_approval={requires_approval}"
        )


def can_execute(*, status: str, requires_approval: bool) -> bool:
    if status in TERMINAL or status == "running":
        return False
    if requires_approval:
        return status == "approved"
    return status in {"proposed", "approved"}


def assert_can_execute(*, status: str, requires_approval: bool) -> None:
    if not can_execute(status=status, requires_approval=requires_approval):
        raise ApprovalDenied(status, requires_approval)
