"""Shared-database schema option (monorepo: ArchOntos in its own schema next to Sion core)."""

from __future__ import annotations

import pytest

from archontos.db.migrate import MigrationError, validate_schema_name
from archontos.db.session import schema_connect_args


def test_schema_connect_args_puts_schema_first() -> None:
    assert schema_connect_args(None) == {}
    assert schema_connect_args("") == {}
    assert schema_connect_args("regulation") == {
        "server_settings": {"search_path": "regulation,public"}
    }


@pytest.mark.parametrize("bad", ["Regulation", "a-b", "x;drop", '"q"', "1abc", "", "a" * 64])
def test_invalid_schema_names_rejected(bad: str) -> None:
    with pytest.raises(MigrationError):
        validate_schema_name(bad)
