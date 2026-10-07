"""Provision and verify the least-privilege database login used by the services.

Usage::

    # as the migrator, after `python -m archontos.db.migrate`:
    ARCHONTOS_APP_DB_PASSWORD=... python -m archontos.db.roles ensure-login --name archontos_svc
    # as the service login (exit 1 if it is superuser / BYPASSRLS / owns tables):
    python -m archontos.db.roles check --dsn postgresql://archontos_svc:...@db/archontos

The password is read from an environment variable, never from argv (visible in ``ps``).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass

import asyncpg

from archontos.db.migrate import plain_dsn

APP_GROUP_ROLE = "archontos_app"
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True, slots=True)
class Privileges:
    role: str
    superuser: bool
    bypass_rls: bool
    owns_tables: int
    in_app_group: bool

    @property
    def problems(self) -> list[str]:
        found = []
        if self.superuser:
            found.append("is superuser (RLS is bypassed)")
        if self.bypass_rls:
            found.append("has BYPASSRLS")
        if self.owns_tables:
            found.append(f"owns {self.owns_tables} table(s) (owners bypass non-FORCE RLS)")
        if not self.in_app_group:
            found.append(f"is not a member of {APP_GROUP_ROLE}")
        return found


async def inspect_current_role(conn: asyncpg.Connection) -> Privileges:
    row = await conn.fetchrow(
        """
        SELECT r.rolname, r.rolsuper, r.rolbypassrls,
               (SELECT count(*) FROM pg_class c
                 WHERE c.relowner = r.oid AND c.relkind IN ('r', 'p')
                   AND c.relnamespace = to_regnamespace(current_schema())::oid) AS owns,
               pg_has_role(r.oid, $1, 'MEMBER') AS member
        FROM pg_roles r WHERE r.rolname = current_user
        """,
        APP_GROUP_ROLE,
    )
    if row is None:  # pragma: no cover - current_user always exists
        raise RuntimeError("current_user not found in pg_roles")
    return Privileges(
        role=row["rolname"],
        superuser=row["rolsuper"],
        bypass_rls=row["rolbypassrls"],
        owns_tables=int(row["owns"]),
        in_app_group=row["member"],
    )


async def ensure_login(conn: asyncpg.Connection, name: str, password: str) -> None:
    if not _ROLE_NAME.fullmatch(name):
        raise ValueError("role name must match [a-z_][a-z0-9_]{0,62}")
    if not password:
        raise ValueError("password must not be empty")
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    verb = "ALTER" if exists else "CREATE"
    # Identifiers cannot be bound; the name is validated above. The password is quoted by the
    # server-side quote_literal so it never needs client-side escaping.
    literal = await conn.fetchval("SELECT quote_literal($1)", password)
    await conn.execute(
        f'{verb} ROLE "{name}" LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE '
        f"PASSWORD {literal}"
    )
    await conn.execute(f'GRANT {APP_GROUP_ROLE} TO "{name}"')


async def verify_service_login(dsn: str, mode: str) -> list[str]:
    """Startup check; raise in ``enforce`` mode when the login can bypass RLS.

    An unreachable database is not a privilege problem: it is logged and startup proceeds
    (readiness probes and the first request will surface it).
    """
    import logging

    log = logging.getLogger("archontos.db")
    if mode == "off":
        return []
    try:
        conn = await asyncpg.connect(plain_dsn(dsn), timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        log.warning("db privilege check skipped: %s", exc)
        return []
    try:
        problems = (await inspect_current_role(conn)).problems
    finally:
        await conn.close()
    if problems and mode == "enforce":
        raise RuntimeError("database login can bypass row-level security: " + "; ".join(problems))
    for problem in problems:
        log.warning("database login %s", problem)
    return problems


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m archontos.db.roles")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("ensure-login", help="create/update the service login role")
    login.add_argument("--name", required=True)
    login.add_argument("--password-env", default="ARCHONTOS_APP_DB_PASSWORD")
    login.add_argument("--dsn", default=None, help="migrator DSN; default ARCHONTOS_DATABASE_URL")
    check = sub.add_parser("check", help="fail if the connecting role can bypass RLS")
    check.add_argument("--dsn", default=None, help="service DSN; default ARCHONTOS_DATABASE_URL")
    args = parser.parse_args(argv)

    dsn = args.dsn or os.getenv("ARCHONTOS_DATABASE_URL")
    if not dsn:
        print("no DSN: pass --dsn or set ARCHONTOS_DATABASE_URL", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(plain_dsn(dsn))
    try:
        if args.command == "ensure-login":
            password = os.getenv(args.password_env, "")
            await ensure_login(conn, args.name, password)
            print(f"login role {args.name} is a member of {APP_GROUP_ROLE}")
            return 0
        privileges = await inspect_current_role(conn)
        if privileges.problems:
            for problem in privileges.problems:
                print(f"{privileges.role}: {problem}", file=sys.stderr)
            return 1
        print(f"{privileges.role}: unprivileged, RLS applies")
        return 0
    finally:
        await conn.close()


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":  # pragma: no cover
    main()
