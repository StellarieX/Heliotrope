"""Run the retained scheduling and normalization E2E scenarios."""
from pathlib import Path
import sys
import pytest

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "backend"))
sys.path.insert(0, str(root))

if __name__ == "__main__":
    files = sorted(str(p) for p in (root / "tests" / "e2e").glob("test_tier*.py"))
    sys.exit(pytest.main([*files, "-q"]))
