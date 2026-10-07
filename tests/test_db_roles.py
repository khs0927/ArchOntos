import asyncio

import pytest

from archontos.db.roles import Privileges, ensure_login, verify_service_login


def test_privilege_problems():
    clean = Privileges("svc", False, False, 0, True)
    assert clean.problems == []
    bad = Privileges("root", True, True, 3, False)
    assert len(bad.problems) == 4


def test_unreachable_database_does_not_block_startup():
    # Port 9 (discard) on loopback: connection refused, not a privilege problem.
    dsn = "postgresql://u:p@127.0.0.1:9/db"
    assert asyncio.run(verify_service_login(dsn, "enforce")) == []


def test_ensure_login_validates_inputs():
    with pytest.raises(ValueError):
        asyncio.run(ensure_login(None, 'bad"name', "pw"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        asyncio.run(ensure_login(None, "svc", ""))  # type: ignore[arg-type]
