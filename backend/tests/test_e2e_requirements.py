"""Run the retained E2E scenarios from the backend suite."""
import subprocess
import sys
from pathlib import Path


def test_e2e_suite():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root / "tests/e2e/runner.py")], cwd=root, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, f"E2E suite failed:\n{result.stdout}\n{result.stderr}"
