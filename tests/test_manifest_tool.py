"""The manifest gate is only worth having if it actually fails.

Each case copies the contract tree to a scratch directory and drives the real
tool, because the defects these cover were all cases of a gate that reported a
problem and then exited zero, or of regen writing a corrupt manifest.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "docs" / "drive-ingestion" / "v4.0"


def _tool_run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "manifest_tool.py", *args], cwd=cwd, capture_output=True, text=True
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    shutil.copytree(SRC, root)
    # A local environment is never part of the contract set.
    shutil.rmtree(root / ".venv-validate", ignore_errors=True)
    return root


def test_a_fresh_copy_is_in_sync(tree: Path) -> None:
    result = _tool_run(tree, "check")
    assert result.returncode == 0, result.stdout
    assert "unlisted=0" in result.stdout


def test_an_unlisted_file_fails_the_gate(tree: Path) -> None:
    """It used to be printed and then discarded by the return statement."""
    (tree / "brand-new-contract.md").write_text("new\n", encoding="utf-8")
    result = _tool_run(tree, "check")
    assert result.returncode == 1
    assert "UNLISTED" in result.stdout


def test_regen_adopts_a_new_file_from_the_tree(tree: Path) -> None:
    """It used to re-hash the old manifest's entry list, so nothing could be added."""
    (tree / "brand-new-contract.md").write_text("new\n", encoding="utf-8")
    assert "brand-new-contract.md" not in (tree / "MANIFEST.sha256").read_text(encoding="utf-8")

    assert _tool_run(tree, "regen").returncode == 0
    assert "brand-new-contract.md" in (tree / "MANIFEST.sha256").read_text(encoding="utf-8")
    assert _tool_run(tree, "check").returncode == 0


def test_a_changed_file_fails_the_gate(tree: Path) -> None:
    target = tree / "ontology.ttl"
    target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    result = _tool_run(tree, "check")
    assert result.returncode == 1
    assert "CHANGED" in result.stdout


def test_regen_refuses_a_deleted_file_and_leaves_the_manifest_intact(tree: Path) -> None:
    """It used to write the literal text None as the digest and exit zero."""
    (tree / "ontology.ttl").unlink()
    before = (tree / "MANIFEST.sha256").read_text(encoding="utf-8")
    result = _tool_run(tree, "regen")
    assert result.returncode == 1
    assert "refusing" in result.stdout
    assert (tree / "MANIFEST.sha256").read_text(encoding="utf-8") == before
    assert "None  ontology.ttl" not in before


def test_a_malformed_manifest_is_a_clean_error_not_a_traceback(tree: Path) -> None:
    (tree / "MANIFEST.sha256").write_text("not-a-digest path\n", encoding="utf-8")
    result = _tool_run(tree, "check")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_an_unknown_mode_is_a_usage_error(tree: Path) -> None:
    assert _tool_run(tree, "bogus").returncode == 2


def test_a_directory_named_like_pycache_notes_is_not_skipped(tree: Path) -> None:
    """The exclusion used to be a substring test on the joined path."""
    nested = tree / "notes"
    nested.mkdir()
    (nested / "pycache_notes.md").write_text("keep me\n", encoding="utf-8")
    result = _tool_run(tree, "check")
    assert result.returncode == 1
    assert "UNLISTED notes/pycache_notes.md" in result.stdout
