"""Heliotrope E2E Test Suite Runner.

Executes all 4 tiers of E2E tests and prints a structured summary report
categorized by tier:
- Tier 1: Feature Coverage (50 tests)
- Tier 2: Boundary & Corner Cases (50 tests)
- Tier 3: Cross-Feature Combinations (15 tests)
- Tier 4: Real-World Application Scenarios (5 tests)

Tier 5 (Adversarial Hardening, 10 tests) runs separately afterwards and is
reported on its own TIER5 line; the 4-tier TOTAL line is unchanged.

Usage:
    python tests/e2e/runner.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
E2E_DIR = REPO_ROOT / "tests" / "e2e"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TIERS = [
    {
        "tier": "Tier 1",
        "name": "Feature Coverage (Core Functional)",
        "file": str(E2E_DIR / "test_tier1_features.py"),
        "target_count": 50,
    },
    {
        "tier": "Tier 2",
        "name": "Boundary & Corner Cases",
        "file": str(E2E_DIR / "test_tier2_boundaries.py"),
        "target_count": 50,
    },
    {
        "tier": "Tier 3",
        "name": "Cross-Feature Combinations",
        "file": str(E2E_DIR / "test_tier3_combinations.py"),
        "target_count": 15,
    },
    {
        "tier": "Tier 4",
        "name": "Real-World Application Scenarios",
        "file": str(E2E_DIR / "test_tier4_scenarios.py"),
        "target_count": 5,
    },
]

#: Tier 5 (adversarial hardening) runs SEPARATELY from Tiers 1-4 and is
#: excluded from the 4-tier TOTAL line below, whose "TOTAL: 120/120"
#: semantics are frozen for the backend bridge
#: (`backend/tests/test_e2e_requirements.py`).
TIER5 = {
    "tier": "Tier 5",
    "name": "Adversarial Hardening",
    "file": str(E2E_DIR / "test_tier5_hardening.py"),
    "target_count": 10,
}


class StructuredReportPlugin:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors = 0

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            if report.passed:
                self.passed += 1
            elif report.failed:
                self.failed += 1
            elif report.skipped:
                self.skipped += 1
        elif report.failed and report.when in ("setup", "teardown"):
            self.errors += 1


def run_tier(tier_info: dict) -> dict:
    plugin = StructuredReportPlugin()
    args = [tier_info["file"], "-q", "--disable-warnings"]
    start_time = time.perf_counter()
    exit_code = pytest.main(args, plugins=[plugin])
    elapsed = time.perf_counter() - start_time

    return {
        "tier": tier_info["tier"],
        "name": tier_info["name"],
        "passed": plugin.passed,
        "failed": plugin.failed,
        "errors": plugin.errors,
        "skipped": plugin.skipped,
        "total": plugin.passed + plugin.failed + plugin.errors + plugin.skipped,
        "target": tier_info["target_count"],
        "elapsed_sec": round(elapsed, 2),
        "status": "PASS" if exit_code == 0 else "FAIL",
    }


def main():
    print("=" * 80)
    print(" HELIOTROPE E2E TEST SUITE RUNNER")
    print(" Executing 4-Tier Comprehensive Test Suite")
    print("=" * 80)

    results = []
    overall_start = time.perf_counter()

    for t in TIERS:
        print(f"\n>> Running {t['tier']}: {t['name']}...")
        r = run_tier(t)
        results.append(r)
        status_symbol = "[OK]" if r["status"] == "PASS" else "[FAIL]"
        print(f"   {status_symbol} {r['passed']}/{r['total']} passed in {r['elapsed_sec']}s")

    total_elapsed = round(time.perf_counter() - overall_start, 2)
    total_passed = sum(r["passed"] for r in results)
    total_failed = sum(r["failed"] for r in results)
    total_errors = sum(r["errors"] for r in results)
    total_tests = sum(r["total"] for r in results)

    print("\n>> Running Tier 5: Adversarial Hardening (separate from 120)...")
    t5 = run_tier(TIER5)
    t5_symbol = "[OK]" if t5["status"] == "PASS" else "[FAIL]"
    print(f"   {t5_symbol} {t5['passed']}/{t5['total']} passed in {t5['elapsed_sec']}s")

    print("\n" + "=" * 80)
    print(f" {'TIER':<10} | {'NAME':<36} | {'STATUS':<6} | {'COUNT':<7} | {'TIME'}")
    print("-" * 80)
    for r in results:
        print(f" {r['tier']:<10} | {r['name']:<36} | {r['status']:<6} | {r['passed']}/{r['target']:<5} | {r['elapsed_sec']}s")
    print("=" * 80)
    print(f" TOTAL: {total_passed}/{total_tests} PASSED across 4 tiers in {total_elapsed}s")
    print(f" TIER5: {t5['passed']}/{t5['target']} PASSED (adversarial hardening, separate from 120)")
    if total_failed == 0 and total_errors == 0 and t5["status"] == "PASS":
        print(" RESULT: 100% PASS (ALL TIERS SATISFIED)")
        print("=" * 80)
        sys.exit(0)
    else:
        print(f" RESULT: FAIL ({total_failed} failed, {total_errors} errors; Tier 5: {t5['status']})")
        print("=" * 80)
        sys.exit(1)


if __name__ == "__main__":
    main()
