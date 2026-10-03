"""Phase 5 end-to-end verification (§52), against a live app instance.

Exercises every endpoint the brief lists and asserts the invariants that must
hold regardless of mode:

    deadline_misses      == 0
    feasibility_violations == 0

Run from the backend directory:  python phase5_verify_endpoints.py
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
START = datetime(2026, 10, 5, tzinfo=timezone.utc)
END = START + timedelta(days=2, minutes=15)

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}{(' — ' + detail) if detail else ''}")


def jobs_payload():
    from tests.fixtures import mixed_scenario

    return [j.model_dump(mode="json") for j in mixed_scenario()]


def actual_signal():
    from app.services.carbon_service import CarbonService

    r = CarbonService(provider_name="synthetic").get_signal(START, END, 15)
    return {
        "start": r.start.isoformat(),
        "end": r.end.isoformat(),
        "resolution_minutes": 15,
        "source": "synthetic_actual",
        "points": [
            {
                "timestamp": p.timestamp.isoformat(),
                "gco2_per_kwh": p.carbon_intensity_gco2_per_kwh,
            }
            for p in r.points
        ],
    }


print("=" * 74)
print("PHASE 5 ENDPOINT VERIFICATION")
print("=" * 74)

# --- GET /api/v1/health ------------------------------------------------------
r = client.get("/api/v1/health")
check("GET /api/v1/health", r.status_code == 200, f"status={r.status_code}")

# --- GET /api/v1/carbon ------------------------------------------------------
r = client.get(
    "/api/v1/carbon",
    params={"start": START.isoformat(), "end": START + timedelta(days=1), "provider": "synthetic"},
)
check(
    "GET /api/v1/carbon",
    r.status_code == 200 and len(r.json()["points"]) == 96,
    f"{len(r.json().get('points', []))} points, signal_type={r.json().get('signal_type')}",
)

# --- POST /api/v1/carbon/forecast --------------------------------------------
r = client.post(
    "/api/v1/carbon/forecast",
    json={"start": START.isoformat(), "end": (START + timedelta(days=1)).isoformat(), "model": "seasonal"},
)
fc = r.json()
ordered = all(
    p["lower_gco2_per_kwh"] <= p["predicted_gco2_per_kwh"] <= p["upper_gco2_per_kwh"]
    for p in fc["points"]
)
check("POST /api/v1/carbon/forecast", r.status_code == 200 and ordered,
      f"{len(fc.get('points', []))} points, intervals ordered={ordered}")

# --- POST /api/v1/carbon/forecast/evaluate -----------------------------------
r = client.post(
    "/api/v1/carbon/forecast/evaluate",
    json={
        "forecast": fc,
        "actual": [
            {"timestamp": p["timestamp"], "gco2_per_kwh": p["predicted_gco2_per_kwh"] + 30.0}
            for p in fc["points"]
        ],
    },
)
m = r.json()["metrics"]
check(
    "POST /api/v1/carbon/forecast/evaluate",
    r.status_code == 200 and abs(m["mae_gco2_per_kwh"] - 30.0) < 1e-6,
    f"MAE={m['mae_gco2_per_kwh']:.3f} bias={m['bias_gco2_per_kwh']:.3f} "
    f"coverage={m['interval_coverage_percent']:.1f}% width={m['interval_width_gco2_per_kwh']:.1f}",
)

# --- POST /api/v1/carbon/forecast/backtest ------------------------------------
r = client.post(
    "/api/v1/carbon/forecast/backtest",
    json={"model": "seasonal", "max_steps": 4, "history_days": 20, "compare_models": True},
)
bt = r.json()["results"]
check(
    "POST /api/v1/carbon/forecast/backtest",
    r.status_code == 200 and set(bt) == {"persistence", "seasonal"},
    "; ".join(
        f"{n}: MAE {v['metrics']['mae_gco2_per_kwh']:.2f} cov "
        f"{v['metrics']['interval_coverage_percent']:.1f}% in {v['runtime_ms']}ms"
        for n, v in bt.items()
    ),
)

# --- POST /api/v1/schedule, every mode ---------------------------------------
print()
print("  scheduling modes (deadline_misses and feasibility_violations must be 0):")
actuals = actual_signal()
for label, carbon_block in (
    ("observed (no carbon block)", None),
    ("EXPECTED", {"mode": "FORECAST", "forecast_model": "seasonal", "forecast_mode": "EXPECTED", "risk_weight": 0.0}),
    ("ROBUST 0.5", {"mode": "FORECAST", "forecast_model": "seasonal", "forecast_mode": "ROBUST", "risk_weight": 0.5}),
    ("ROBUST 1.0", {"mode": "FORECAST", "forecast_model": "seasonal", "forecast_mode": "ROBUST", "risk_weight": 1.0}),
):
    body = {"jobs": jobs_payload(), "capacity_kw": 10.0, "scheduler": "CPSAT",
            "solver_config": {"time_limit_seconds": 5.0}}
    if carbon_block:
        body["carbon"] = {**carbon_block, "actual_signal": actuals}
    r = client.post("/api/v1/schedule", json=body)
    d = r.json()
    ok = (
        r.status_code == 200
        and d["status"] in ("FEASIBLE", "OPTIMAL")
        and d["metrics"]["deadline_misses"] == 0
        and d["metrics"]["feasibility_violations"] == 0
    )
    realized = (d.get("realized") or {}).get("realized_co2_kg")
    check(
        f"POST /api/v1/schedule [{label}]",
        ok,
        f"status={d['status']} fcst_co2={d['metrics']['total_co2_kg']:.4f} kg"
        + (f" realized={realized:.4f} kg" if realized else "")
        + f" misses={d['metrics']['deadline_misses']} viol={d['metrics']['feasibility_violations']}",
    )

print()
failed = [n for n, ok, _ in results if not ok]
print("=" * 74)
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    for n in failed:
        print(f"  FAILED: {n}")
print("=" * 74)