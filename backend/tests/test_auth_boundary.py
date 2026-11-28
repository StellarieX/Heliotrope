from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi import HTTPException

from app.core import auth, config


def test_unconfigured_execution_rejects_requests(monkeypatch):
    monkeypatch.setattr(config, 'SUPABASE_URL', '')
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.verified_user('Bearer token'))
    assert error.value.status_code == 503


def test_recent_auth_uses_authentication_time_not_refresh_time():
    now = time.time()
    stale = auth.RequestContext('owner', {'iat': now, 'amr': [{'method': 'oauth', 'timestamp': now - 600}]})
    with pytest.raises(HTTPException) as error:
        auth.require_recent_auth(stale)
    assert error.value.status_code == 403
    auth.require_recent_auth(auth.RequestContext('owner', {'amr': [{'method': 'oauth', 'timestamp': now}]}))


def test_auth_rejects_forged_and_unavailable(monkeypatch):
    monkeypatch.setattr(config, 'SUPABASE_URL', 'https://example.test')
    monkeypatch.setattr(config, 'SUPABASE_PUBLISHABLE_KEY', 'public')

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs):
            return httpx.Response(401, request=httpx.Request('GET', 'https://example.test'))

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: Client())
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.verified_user('Bearer forged'))
    assert error.value.status_code == 401


def test_verified_user_binds_token_subject_to_remote_user(monkeypatch):
    import base64
    import json
    monkeypatch.setattr(config, 'SUPABASE_URL', 'https://example.test')
    monkeypatch.setattr(config, 'SUPABASE_PUBLISHABLE_KEY', 'public')
    owner = '11111111-1111-4111-8111-111111111111'
    payload = base64.urlsafe_b64encode(json.dumps({'sub': owner}).encode()).decode().rstrip('=')

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs):
            return httpx.Response(200, json={'id': owner}, request=httpx.Request('GET', 'https://example.test'))

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: Client())
    user = asyncio.run(auth.verified_user(f'Bearer header.{payload}.signature'))
    assert user.owner_id == owner
    other = base64.urlsafe_b64encode(json.dumps({'sub': 'different'}).encode()).decode().rstrip('=')
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.verified_user(f'Bearer header.{other}.signature'))
    assert error.value.status_code == 401


def test_execution_routes_fail_closed_when_auth_unconfigured(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    app.dependency_overrides.pop(auth.execution_session, None)
    monkeypatch.setattr(config, 'SUPABASE_URL', '')
    client = TestClient(app)
    assert client.get('/api/v1/schedules/active').status_code == 503
    assert client.get('/api/v1/schedules/other/state').status_code == 503
