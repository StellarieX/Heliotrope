"""Forecast and robust-scheduling API (Phase 5, §39, §40, §43, §52).

Endpoint-level tests. The service-level behaviour is covered in
`test_forecasting.py`, `test_forecast_evaluation.py` and
`test_robust_scheduling.py`; what is checked here is the HTTP contract:

  * structured responses, no hardcoded numbers (§8)
  * 400 for an unknown model, never a substitution (§28, §34)
  * 422 for malformed input, 422 for a backtest that is too big (§40)
  * `/schedule` stays backward compatible with no `carbon` block (§38)
  * every successful schedule still reports zero violations and zero deadline
    misses, in every mode (§43)
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

from .fixtures import DAY_START, mixed_scenario

client = TestClient(app)

WINDOW_START = datetime(2026, 10, 5, tzinfo=timezone.utc)
WINDOW_END = WINDOW_START + timedelta(days=1)


def forecast_request(**overrides):
    body = {
        "start": WINDOW_START.isoformat(),
        "end": WINDOW_END.isoformat(),
        "resolution_minutes": 15,
        "model": "seasonal",
    }
    body.update(overrides)
    return body


def jobs_payload():
    return [job.model_dump(mode="json") for job in mixed_scenario()]


def actual_signal(start, end):
    from app.services.carbon_service import CarbonService

    response = CarbonService(provider_name="synthetic").get_signal(start, end, 15)
    return {
        "start": response.start.isoformat(),
        "end": response.end.isoformat(),
        "resolution_minutes": 15,
        "source": "synthetic_actual",
        "points": [
            {
                "timestamp": p.timestamp.isoformat(),
                "gco2_per_kwh": p.carbon_intensity_gco2_per_kwh,
            }
            for p in response.points
        ],
    }


# --- §8, §39 POST /carbon/forecast --------------------------------------------


def test_forecast_endpoint_returns_points_and_provenance():
    response = client.post("/api/v1/carbon/forecast", json=forecast_request())
    assert response.status_code == 200
    payload = response.json()
    assert payload["signal_type"] == "FORECAST"
    assert len(payload["points"]) == 96
    for point in payload["points"]:
        assert (
            point["lower_gco2_per_kwh"]
            <= point["predicted_gco2_per_kwh"]
            <= point["upper_gco2_per_kwh"]
        )
    provenance = payload["provenance"]
    for key in (
        "model",
        "generated_at",
        "training_window_start",
        "training_window_end",
        "source_signal",
        "horizon_start",
        "horizon_end",
        "resolution_minutes",
    ):
        assert key in provenance, key


def test_forecast_endpoint_never_claims_a_confidence_level():
    response = client.post("/api/v1/carbon/forecast", json=forecast_request())
    assert "confidence" not in response.text.lower()


def test_forecast_endpoint_honours_the_resolution():
    response = client.post(
        "/api/v1/carbon/forecast", json=forecast_request(resolution_minutes=60)
    )
    assert response.status_code == 200
    assert len(response.json()["points"]) == 24


def test_forecast_endpoint_rejects_an_unknown_model_with_400():
    """§28: a request for a model Heliotrope does not have must not silently get
    a different one."""
    response = client.post("/api/v1/carbon/forecast", json=forecast_request(model="xgboost"))
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_model"


@pytest.mark.parametrize(
    "overrides",
    [
        {"start": "not-a-timestamp"},
        {"end": "not-a-timestamp"},
        {"start": "2026-10-05T00:00:00"},  # naive
        {"start": WINDOW_END.isoformat(), "end": WINDOW_START.isoformat()},
        {"coverage": 0.0},
        {"coverage": 1.5},
        {"resolution_minutes": 1},
    ],
)
def test_forecast_endpoint_returns_422_for_bad_input(overrides):
    response = client.post("/api/v1/carbon/forecast", json=forecast_request(**overrides))
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_forecast_endpoint_refuses_a_window_longer_than_its_cap():
    response = client.post(
        "/api/v1/carbon/forecast",
        json=forecast_request(end=(WINDOW_START + timedelta(days=30)).isoformat()),
    )
    assert response.status_code == 422
    assert "at most" in response.json()["detail"]


def test_forecast_is_reproducible_given_the_same_request():
    first = client.post("/api/v1/carbon/forecast", json=forecast_request()).json()
    second = client.post("/api/v1/carbon/forecast", json=forecast_request()).json()
    assert [p["predicted_gco2_per_kwh"] for p in first["points"]] == [
        p["predicted_gco2_per_kwh"] for p in second["points"]
    ]


# --- §13 POST /carbon/forecast/evaluate --------------------------------------


def test_evaluate_endpoint_scores_an_inline_forecast():
    forecast = client.post("/api/v1/carbon/forecast", json=forecast_request()).json()
    actual = [
        {
            "timestamp": p["timestamp"],
            "gco2_per_kwh": p["predicted_gco2_per_kwh"] + 25.0,
        }
        for p in forecast["points"]
    ]
    response = client.post(
        "/api/v1/carbon/forecast/evaluate",
        json={"forecast": forecast, "actual": actual},
    )
    assert response.status_code == 200
    metrics = response.json()["metrics"]
    assert metrics["evaluated_points"] == 96
    # Actuals are uniformly 25 g/kWh higher, so the bias must be +25 and the MAE 25.
    assert metrics["bias_gco2_per_kwh"] == pytest.approx(25.0, abs=1e-6)
    assert metrics["mae_gco2_per_kwh"] == pytest.approx(25.0, abs=1e-6)
    assert metrics["interval_coverage_percent"] is not None
    assert metrics["interval_width_gco2_per_kwh"] is not None


def test_evaluate_endpoint_rebuilds_a_forecast_when_none_is_supplied():
    response = client.post(
        "/api/v1/carbon/forecast/evaluate",
        json=forecast_request(),
    )
    assert response.status_code == 200
    assert response.json()["metrics"]["evaluated_points"] == 0
    assert response.json()["metrics"]["mae_gco2_per_kwh"] is None


def test_evaluate_endpoint_returns_422_without_a_forecast_or_window():
    response = client.post("/api/v1/carbon/forecast/evaluate", json={"actual": []})
    assert response.status_code == 422


def test_evaluate_endpoint_returns_422_for_a_malformed_forecast():
    response = client.post(
        "/api/v1/carbon/forecast/evaluate",
        json={"forecast": {"points": [], "provenance": {}}, "actual": []},
    )
    assert response.status_code == 422


def test_evaluate_endpoint_counts_missing_actuals():
    forecast = client.post("/api/v1/carbon/forecast", json=forecast_request()).json()
    actual = [
        {"timestamp": p["timestamp"], "gco2_per_kwh": 300.0} for p in forecast["points"][:10]
    ]
    response = client.post(
        "/api/v1/carbon/forecast/evaluate",
        json={"forecast": forecast, "actual": actual},
    )
    metrics = response.json()["metrics"]
    assert metrics["evaluated_points"] == 10
    assert metrics["missing_actuals"] == 86


# --- §40 POST /carbon/forecast/backtest --------------------------------------


def test_backtest_endpoint_runs_on_its_synthetic_dataset():
    response = client.post(
        "/api/v1/carbon/forecast/backtest",
        json={"model": "seasonal", "max_steps": 3, "history_days": 20},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["steps"] > 0
    assert payload["runtime_ms"] >= 0
    assert payload["metrics"]["mae_gco2_per_kwh"] is not None
    assert payload["metrics"]["interval_coverage_percent"] is not None


def test_backtest_endpoint_can_compare_both_models():
    response = client.post(
        "/api/v1/carbon/forecast/backtest",
        json={"model": "seasonal", "max_steps": 3, "history_days": 20, "compare_models": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["results"]) == {"persistence", "seasonal"}
    assert "no ranking is implied" in payload["note"].lower()


def test_backtest_endpoint_bounds_a_cheap_request():
    """§40. An unbounded backtest endpoint is a denial-of-service vector."""
    response = client.post(
        "/api/v1/carbon/forecast/backtest", json={"max_steps": 5000}
    )
    assert response.status_code == 422


def test_backtest_endpoint_rejects_an_unknown_model():
    response = client.post("/api/v1/carbon/forecast/backtest", json={"model": "lstm"})
    assert response.status_code == 400


def test_backtest_endpoint_accepts_a_supplied_dataset():
    from app.services.forecasting import SyntheticCarbonHistory

    history = SyntheticCarbonHistory().history(WINDOW_START, days=21)
    points = [
        {"timestamp": p.time.isoformat(), "gco2_per_kwh": p.gco2_per_kwh}
        for p in history.points
    ]
    response = client.post(
        "/api/v1/carbon/forecast/backtest",
        json={"history": points, "model": "seasonal", "max_steps": 3, "lookback_days": 7},
    )
    assert response.status_code == 200
    assert response.json()["steps"] > 0


def test_backtest_endpoint_rejects_unsorted_history():
    """§26: a shuffled or reversed series is refused, not silently sorted."""
    response = client.post(
        "/api/v1/carbon/forecast/backtest",
        json={
            "history": [
                {"timestamp": (WINDOW_START + timedelta(minutes=15 * i)).isoformat(),
                 "gco2_per_kwh": 300.0}
                for i in reversed(range(200))
            ],
            "model": "seasonal",
            "max_steps": 2,
        },
    )
    assert response.status_code == 422
    assert "time-ordered" in response.json()["detail"]


# --- §38 POST /schedule with and without a forecast --------------------------


def test_schedule_still_works_with_no_carbon_block():
    """§38 backward compatibility: the Phase 4 request shape must be untouched."""
    response = client.post(
        "/api/v1/schedule",
        json={"jobs": jobs_payload(), "capacity_kw": 10.0, "scheduler": "CPSAT"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in ("FEASIBLE", "OPTIMAL")
    assert payload["forecast"]["mode"] == "ACTUAL"
    assert payload["metrics"]["feasibility_violations"] == 0
    assert payload["metrics"]["deadline_misses"] == 0


@pytest.mark.parametrize(
    "mode,risk_weight",
    [("EXPECTED", 0.0), ("ROBUST", 0.5), ("ROBUST", 1.0)],
)
@pytest.mark.parametrize("scheduler", ["CPSAT", "GREEDY"])
def test_schedule_with_a_forecast_never_reports_a_violation(mode, risk_weight, scheduler):
    """§43: whatever the forecast mode, the hard constraints still hold."""
    response = client.post(
        "/api/v1/schedule",
        json={
            "jobs": jobs_payload(),
            "capacity_kw": 10.0,
            "scheduler": scheduler,
            "carbon": {
                "mode": "FORECAST",
                "forecast_model": "seasonal",
                "forecast_mode": mode,
                "risk_weight": risk_weight,
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in ("FEASIBLE", "OPTIMAL"), payload.get("reason")
    assert payload["metrics"]["feasibility_violations"] == 0
    assert payload["metrics"]["deadline_misses"] == 0
    assert payload["violations"] == []


def test_schedule_response_states_what_it_scheduled_against():
    response = client.post(
        "/api/v1/schedule",
        json={
            "jobs": jobs_payload(),
            "capacity_kw": 10.0,
            "scheduler": "CPSAT",
            "carbon": {
                "mode": "FORECAST",
                "forecast_model": "seasonal",
                "forecast_mode": "ROBUST",
                "risk_weight": 0.5,
                "deadline_buffer_minutes": 15,
            },
        },
    )
    payload = response.json()
    summary = payload["forecast"]
    assert summary["mode"] == "FORECAST"
    assert summary["forecast_mode"] == "ROBUST"
    assert summary["risk_weight"] == 0.5
    assert summary["deadline_buffer_minutes"] == 15
    assert summary["model"] == "seasonal"
    assert summary["training_window"]
    assert "not whether" in summary["note"]


def test_schedule_rejects_a_partial_slot_deadline_buffer():
    response = client.post(
        "/api/v1/schedule",
        json={
            "jobs": jobs_payload(),
            "capacity_kw": 10.0,
            "scheduler": "CPSAT",
            "carbon": {"mode": "FORECAST", "deadline_buffer_minutes": 7},
        },
    )
    assert response.status_code == 422


def test_schedule_rejects_an_out_of_range_risk_weight():
    response = client.post(
        "/api/v1/schedule",
        json={
            "jobs": jobs_payload(),
            "capacity_kw": 10.0,
            "scheduler": "CPSAT",
            "carbon": {"mode": "FORECAST", "forecast_mode": "ROBUST", "risk_weight": 99.0},
        },
    )
    assert response.status_code == 422


# --- §35 realized CO2 through the API ----------------------------------------


def test_schedule_reports_realized_co2_when_actuals_are_supplied():
    start = DAY_START
    end = DAY_START + timedelta(days=2, minutes=15)
    response = client.post(
        "/api/v1/schedule",
        json={
            "jobs": jobs_payload(),
            "capacity_kw": 10.0,
            "scheduler": "CPSAT",
            "carbon": {
                "mode": "FORECAST",
                "forecast_model": "seasonal",
                "forecast_mode": "ROBUST",
                "risk_weight": 0.5,
                "actual_signal": actual_signal(start, end),
            },
        },
    )
    assert response.status_code == 200
    realized = response.json()["realized"]
    assert realized["realized_co2_kg"] is not None
    assert realized["realized_co2_kg"] > 0
    assert realized["forecast_mode"] == "ROBUST"
    assert realized["feasibility_violations"] == 0
    assert realized["deadline_misses"] == 0


def test_realized_co2_is_absent_when_no_actuals_are_supplied():
    """Without actuals the response must NOT present a realized number."""
    response = client.post(
        "/api/v1/schedule",
        json={
            "jobs": jobs_payload(),
            "capacity_kw": 10.0,
            "scheduler": "CPSAT",
            "carbon": {"mode": "FORECAST"},
        },
    )
    assert "realized" not in response.json()


def test_a_malformed_actual_signal_reports_no_realized_co2():
    response = client.post(
        "/api/v1/schedule",
        json={
            "jobs": jobs_payload(),
            "capacity_kw": 10.0,
            "scheduler": "CPSAT",
            "carbon": {
                "mode": "FORECAST",
                "actual_signal": {
                    "start": "nonsense",
                    "end": "nonsense",
                    "points": [{"timestamp": "also-nonsense", "gco2_per_kwh": 300.0}],
                },
            },
        },
    )
    assert response.status_code == 200
    realized = response.json()["realized"]
    assert realized["realized_co2_kg"] is None
    assert "malformed" in realized["error"]
    assert "not against reality" in realized["note"]


# --- §52 the endpoints exist --------------------------------------------------


def test_all_phase_five_endpoints_are_registered():
    """Checked through the OpenAPI schema, which is what a client actually sees."""
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/v1/carbon/forecast",
        "/api/v1/carbon/forecast/evaluate",
        "/api/v1/carbon/forecast/backtest",
        "/api/v1/schedule",
    ):
        assert path in paths, path


def test_the_observed_carbon_endpoint_is_unchanged():
    response = client.get(
        "/api/v1/carbon",
        params={
            "start": WINDOW_START.isoformat(),
            "end": WINDOW_END.isoformat(),
            "provider": "synthetic",
        },
    )
    assert response.status_code == 200
    assert len(response.json()["points"]) == 96