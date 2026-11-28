"""Exercise the app against the local Supabase stack and its real Auth API."""

from datetime import datetime, timedelta, timezone
import os
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient


def test_supabase_account_and_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.environ.get("SUPABASE_URL", "")
    admin_key = os.environ.get("SUPABASE_SECRET_KEY", "")
    publishable_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    dsn = os.environ.get("DATABASE_URL", "")
    if not all((url, admin_key, publishable_key, dsn)):
        pytest.skip("Start the local Supabase stack and set its connection settings.")
    if any(urlsplit(value).hostname not in {"localhost", "127.0.0.1", "::1"} for value in (url, dsn)):
        pytest.fail("This test only creates accounts in a loopback Supabase stack.")

    from app.api.routes import execution
    from app.core import config
    from app.main import app
    from app.services.postgres_execution_store import PostgresExecutionStore
    from backend.scripts.import_legacy import apply, prepare

    monkeypatch.setattr(config, "SUPABASE_URL", url.rstrip("/"))
    monkeypatch.setattr(config, "SUPABASE_PUBLISHABLE_KEY", publishable_key)
    monkeypatch.setattr(config, "SUPABASE_SECRET_KEY", admin_key)
    monkeypatch.setattr(config, "DATABASE_URL", dsn)
    admin_headers = {"apikey": admin_key, "Authorization": f"Bearer {admin_key}"}
    created: list[str] = []

    with httpx.Client(base_url=url, timeout=10) as supabase, TestClient(app, raise_server_exceptions=False) as client:
        def login_test_account() -> tuple[str, dict[str, str]]:
            email = f"heliotrope-{uuid4().hex}@example.com"
            password = uuid4().hex
            response = supabase.post(
                "/auth/v1/admin/users", headers=admin_headers,
                json={"email": email, "password": password, "email_confirm": True},
            )
            response.raise_for_status()
            uid = response.json()["id"]
            created.append(uid)
            response = supabase.post(
                "/auth/v1/token?grant_type=password", headers={"apikey": publishable_key},
                json={"email": email, "password": password},
            )
            response.raise_for_status()
            return uid, {"Authorization": "Bearer " + response.json()["access_token"]}

        try:
            owner, owner_headers = login_test_account()
            other, other_headers = login_test_account()
            public_headers = {"apikey": publishable_key}
            username = "smoke_" + uuid4().hex[:12]
            response = supabase.post(
                "/rest/v1/rpc/complete_onboarding", headers={**public_headers, **owner_headers},
                json={
                    "p_username": username, "p_display_name": "Local verification", "p_photo_url": None,
                    "p_occupation": "Student", "p_place": "Home", "p_rooms": 2, "p_max_power_kw": 10,
                    "p_jobs": [{"name": "EV", "powerKw": 7.2, "jobType": "DEFERRABLE_INTERRUPTIBLE"}],
                },
            )
            assert response.status_code == 204, response.text
            response = supabase.get(
                "/rest/v1/profile_directory", headers=public_headers,
                params={"select": "username,display_name,photo_url", "username": f"eq.{username}"},
            )
            assert response.json() == [{"username": username, "display_name": "Local verification", "photo_url": None}]

            release = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            deadline = release + timedelta(hours=4)
            plan = {
                "jobs": [{
                    "id": "ev", "normalized_name": "EV", "category": "EV charging",
                    "job_type": "DEFERRABLE_INTERRUPTIBLE",
                    "power_kw": 7.2, "max_power_kw": 7.2, "energy_required_kwh": 7.2,
                    "release_at": release.isoformat(), "deadline_at": deadline.isoformat(),
                }],
                "capacity_kw": 10, "scheduler": "ASAP", "carbon_provider": "synthetic", "share_pool": True,
            }
            assert client.get("/api/v1/schedules/active").status_code == 401
            response = client.post("/api/v1/schedules/plan", json=plan, headers=owner_headers)
            assert response.status_code == 200, response.text
            first_schedule = response.json()["schedule_id"]

            monkeypatch.setattr(execution, "store", PostgresExecutionStore())
            response = client.get("/api/v1/schedules/active", headers=owner_headers)
            assert response.json()["schedule_id"] == first_schedule
            state_path = f"/api/v1/schedules/{first_schedule}/state"
            assert client.get(state_path, headers=other_headers).status_code == 404

            with monkeypatch.context() as invalid_write:
                new_record = execution.store._new_record
                invalid_write.setattr(execution.store, "_new_record", lambda _id, *args: new_record("invalid", *args))
                response = client.post(
                    "/api/v1/schedules/plan", json={**plan, "replaces_schedule_id": first_schedule}, headers=owner_headers,
                )
                assert response.status_code == 500
            assert client.get(state_path, headers=owner_headers).json()["lifecycle"] == "SCHEDULED"

            response = client.post(
                "/api/v1/schedules/plan", json={**plan, "replaces_schedule_id": first_schedule}, headers=owner_headers,
            )
            assert response.status_code == 200, response.text
            assert client.get(state_path, headers=owner_headers).json()["lifecycle"] == "CANCELLED"

            # Reuse a real serialized plan as a fixture for the historical import.
            legacy_id = uuid4().hex[:12]
            with psycopg.connect(dsn) as connection:
                previous = connection.execute(
                    "SELECT data FROM private.execution_schedules WHERE schedule_id = %s", (first_schedule,)
                ).fetchone()[0]
                previous["schedule_id"] = legacy_id
                apply(connection, prepare(
                    {"schedules": [{"schedule_id": legacy_id, "lifecycle": "CANCELLED", "data": previous}]},
                    {"users": {"legacy-owner": other}, "schedules": {legacy_id: "legacy-owner"}},
                ))
            legacy_path = f"/api/v1/schedules/{legacy_id}/history"
            assert client.get(legacy_path, headers=other_headers).status_code == 200
            assert client.get(legacy_path, headers=owner_headers).status_code == 404

            response = client.delete("/api/v1/account", headers=owner_headers)
            assert response.status_code == 204, response.text
            created.remove(owner)
            response = supabase.get(
                "/rest/v1/profile_directory", headers=public_headers,
                params={"select": "username", "username": f"eq.{username}"},
            )
            assert response.json() == []
            assert client.get("/api/v1/schedules/active", headers=owner_headers).status_code == 401
        finally:
            for uid in created:
                supabase.delete(f"/auth/v1/admin/users/{uid}", headers=admin_headers).raise_for_status()
