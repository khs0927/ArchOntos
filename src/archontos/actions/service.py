from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ProposedAction:
    action_type: str
    target_refs: list[str]
    input_payload: dict[str, Any]
    proposed_output: dict[str, Any]
    requires_approval: bool = True


def propose_report(target_refs: list[str], context: dict[str, Any]) -> ProposedAction:
    return ProposedAction(
        action_type="Report",
        target_refs=target_refs,
        input_payload=context,
        proposed_output={"format": "json", "status": "proposal"},
        requires_approval=True,
    )
