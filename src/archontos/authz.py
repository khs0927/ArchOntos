"""Role-based authorization for the service APIs.

Configured with ``ARCHONTOS_ACTOR_ROLES`` (``alice:approver+executor,bob:proposer``). When it
is empty, authorization is disabled and every caller may do everything (local MVP-0, and
the behaviour before this module existed). When it is set:

* only authenticated principals (named API keys) are authorised; anonymous callers get 401;
* an actor without a listed role has no permissions (403);
* four-eyes: the actor who proposed an action may not approve it.

``ARCHONTOS_ACTOR_JURISDICTIONS`` (``alice:KR+JP``) narrows or widens the jurisdictions an
actor's transactions run with; row-level security (migration 013) enforces them.
"""

from __future__ import annotations

from enum import StrEnum

from archontos.identity import InvalidActor, Principal, validate_actor


class Permission(StrEnum):
    ACTION_READ = "action.read"
    ACTION_PROPOSE = "action.propose"
    ACTION_APPROVE = "action.approve"
    ACTION_REJECT = "action.reject"
    ACTION_EXECUTE = "action.execute"
    SEARCH_QUERY = "search.query"
    HYPEREDGE_WRITE = "hyperedge.write"
    PROJECTION_ADMIN = "projection.admin"
    QUERY_READ = "query.read"
    SOURCE_INGEST = "source.ingest"
    ASSERTION_PROPOSE = "assertion.propose"
    ASSERTION_REVIEW = "assertion.review"
    RULE_COMPILE = "rule.compile"


_VIEWER = frozenset({Permission.ACTION_READ, Permission.SEARCH_QUERY, Permission.QUERY_READ})

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": _VIEWER,
    "proposer": _VIEWER | {Permission.ACTION_PROPOSE},
    "approver": _VIEWER | {Permission.ACTION_APPROVE, Permission.ACTION_REJECT},
    "executor": _VIEWER | {Permission.ACTION_EXECUTE},
    "operator": _VIEWER | {Permission.HYPEREDGE_WRITE, Permission.PROJECTION_ADMIN},
    # Legal-knowledge pipeline: curators bring sources and candidate assertions in;
    # reviewers accept/reject assertions and compile approved ones into rules.
    "curator": _VIEWER | {Permission.SOURCE_INGEST, Permission.ASSERTION_PROPOSE},
    "reviewer": _VIEWER | {Permission.ASSERTION_REVIEW, Permission.RULE_COMPILE},
    "admin": frozenset(Permission),
}


class AuthorizationError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _parse_mapping(raw: str, what: str) -> dict[str, frozenset[str]]:
    mapping: dict[str, frozenset[str]] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        actor, sep, values = entry.partition(":")
        if not sep or not actor.strip():
            raise ValueError(f"{what} entries must look like actor:value+value, got {entry!r}")
        try:
            name = validate_actor(actor.strip())
        except InvalidActor as exc:
            raise ValueError(str(exc)) from exc
        mapping[name] = frozenset(v.strip() for v in values.split("+") if v.strip())
    return mapping


def parse_actor_roles(raw: str) -> dict[str, frozenset[str]]:
    mapping = _parse_mapping(raw, "ARCHONTOS_ACTOR_ROLES")
    for actor, roles in mapping.items():
        unknown = roles - ROLE_PERMISSIONS.keys()
        if unknown:
            raise ValueError(f"unknown role(s) {sorted(unknown)} for actor {actor!r}")
    return mapping


def parse_actor_jurisdictions(raw: str) -> dict[str, tuple[str, ...]]:
    return {
        actor: tuple(sorted(codes))
        for actor, codes in _parse_mapping(raw, "ARCHONTOS_ACTOR_JURISDICTIONS").items()
    }


def permissions_for(roles: frozenset[str]) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def authorize(principal: Principal | None, permission: Permission, *, enabled: bool) -> None:
    if not enabled:
        return
    if principal is None or not principal.authenticated:
        raise AuthorizationError(401, "authentication required")
    if permission not in permissions_for(principal.roles):
        raise AuthorizationError(403, f"{principal.actor!r} lacks permission {permission.value}")


def check_four_eyes(principal: Principal | None, proposer: str, *, enabled: bool) -> None:
    if enabled and principal is not None and principal.actor == proposer:
        raise AuthorizationError(403, "the proposer of an action cannot approve it")
