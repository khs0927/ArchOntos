"""The manifest gate is only worth wiring into CI if the job can actually fail.

These tests read the workflow for the properties that make the gate real, and
run the exact shell the job runs against the real tool, because a gate that
reports drift and exits zero is the failure mode this whole change is about.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
TOOL_DIR = REPO / "docs" / "drive-ingestion" / "v4.0"

# The exit-code contract the job depends on. The job wraps this in a shell
# `case`; on this host that wrapper cannot be executed because a Windows
# interpreter path is not runnable from bash, so the contract is asserted
# directly here and the wrapper is asserted structurally.
DRIFT_MARKERS = ("CHANGED", "MISSING", "UNLISTED")


def _run_tool(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "manifest_tool.py", *args], cwd=cwd, capture_output=True, text=True
    )


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _jobs() -> list[str]:
    return re.findall(r"^  (\w[\w-]*):$", _workflow(), re.M)


def _run_job(cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run_tool(cwd, "check")


def test_manifest_is_an_independent_job() -> None:
    assert "manifest" in _jobs()
    assert "test" in _jobs()
    assert "working-directory: docs/drive-ingestion/v4.0" in _workflow()


def test_the_gate_cannot_be_weakened() -> None:
    text = _workflow()
    # Match the YAML key, not the phrase: the workflow's own comment about
    # this rule contains the words.
    assert "continue-on-error:" not in text
    assert "if: failure" not in text
    # A path filter creates a way to change the gate or the files it guards
    # without it ever running.
    assert "paths:" not in text
    assert "paths-ignore:" not in text
    # The original gates must survive.
    assert "permissions:" in text and "contents: read" in text
    for step in ("ruff check .", "pytest", "compileall -q src apps tests"):
        assert step in text


def test_triggers_are_unchanged() -> None:
    text = _workflow()
    assert "workflow_dispatch:" in text
    assert "pull_request:" in text
    assert "branches: [main]" in text


def test_job_passes_on_a_clean_tree() -> None:
    result = _run_tool(TOOL_DIR, "check")
    assert result.returncode == 0, result.stdout
    assert "unlisted=0" in result.stdout


def test_job_fails_on_drift(tmp_path: Path) -> None:
    copy = tmp_path / "v4.0"
    shutil.copytree(TOOL_DIR, copy)
    shutil.rmtree(copy / ".venv-validate", ignore_errors=True)
    (copy / "ontology.ttl").unlink()

    result = _run_tool(copy, "check")
    assert result.returncode == 1, result.stdout
    assert any(marker in result.stdout for marker in DRIFT_MARKERS)
    assert "regen" in result.stdout, "the job tells the user how to fix drift"


def test_usage_error_is_distinguishable_from_drift(tmp_path: Path) -> None:
    """Exit 2 must not be read as drift, or a broken tool looks like bad data."""
    copy = tmp_path / "v4.0"
    shutil.copytree(TOOL_DIR, copy)
    shutil.rmtree(copy / ".venv-validate", ignore_errors=True)
    (copy / "MANIFEST.sha256").write_text("garbage\n", encoding="utf-8")

    assert _run_tool(copy, "check").returncode == 2
    assert _run_tool(copy, "bogus-mode").returncode == 2


def test_the_job_wraps_those_exit_codes() -> None:
    text = _workflow()
    # The job must branch on the code, not simply run the tool and inherit it,
    # so a usage error cannot be reported as drift or silently ignored.
    assert "set +e" in text
    assert 'case "$code" in' in text
    assert "::error::" in text
    assert "regen" in text, "the job must say how to fix drift"
