"""The wheel ships every package and the SQL migrations (non-editable / git installs)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from archontos.db.migrate import default_migrations_dir, discover, packaged_migrations_dir

ROOT = Path(__file__).resolve().parents[1]


def _setuptools() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["tool"]["setuptools"]


def test_package_list_matches_src_tree() -> None:
    on_disk = {
        ".".join(p.relative_to(ROOT / "src").parts)
        for p in (ROOT / "src" / "archontos").rglob("*")
        if p.is_dir() and p.name != "__pycache__" and any(p.glob("*.py"))
    } | {"archontos"}
    listed = set(_setuptools()["packages"]) - {"archontos._migrations"}
    assert listed == on_disk


def test_migrations_are_package_data() -> None:
    cfg = _setuptools()
    assert cfg["package-dir"]["archontos._migrations"] == "db/migrations"
    assert cfg["package-data"]["archontos._migrations"] == ["*.sql"]


def test_packaged_migrations_match_checkout_when_installed() -> None:
    packaged = packaged_migrations_dir()
    if packaged is None:  # plain source checkout without an install of the package data
        return
    names = sorted(p.name for p in packaged.glob("*.sql"))
    assert names == sorted(p.name for p in (ROOT / "db" / "migrations").glob("*.sql"))


def test_default_dir_falls_back_to_packaged(monkeypatch, tmp_path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "001_x.sql").write_text("select 1;", encoding="utf-8")
    monkeypatch.delenv("ARCHONTOS_MIGRATIONS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("archontos.db.migrate.Path.is_dir", lambda self: self == pkg)
    monkeypatch.setattr("archontos.db.migrate.packaged_migrations_dir", lambda: pkg)
    assert default_migrations_dir() == pkg
    assert [m.version for m in discover(pkg)] == ["001_x"]
