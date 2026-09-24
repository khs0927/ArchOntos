from uuid import UUID

from pydantic import BaseModel


class RuleCompilationView(BaseModel):
    rule_id: UUID
    rule_version_id: UUID
    assertion_id: UUID
    status: str
    created: bool
