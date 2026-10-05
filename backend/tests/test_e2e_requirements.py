"""Bridge test module to execute E2E requirements suite from backend/tests.

Executes all 120 tests across Tiers 1-4 via the dedicated E2E runner.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
_runner_script = _repo_root / "tests" / "e2e" / "runner.py"


def test_e2e_suite_all_tiers_pass():
    """Executes the full 4-tier E2E suite and verifies 100% pass across all 120 tests."""
    result = subprocess.run(
        [sys.executable, str(_runner_script)],
        cwd=str(_repo_root),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"E2E runner exited with error:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "TOTAL: 120/120 PASSED" in result.stdout
    assert "RESULT: 100% PASS (ALL TIERS SATISFIED)" in result.stdout


def test_tier5_pass():
    """Runs the Tier 5 adversarial hardening suite (separate from the 120) and verifies 100% pass."""
    tier5_file = _repo_root / "tests" / "e2e" / "test_tier5_hardening.py"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tier5_file), "-q"],
        cwd=str(_repo_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"Tier 5 suite failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "10 passed" in result.stdout
