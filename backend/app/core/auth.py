from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx
from fastapi import Depends, Header, HTTPException
from psycopg import Connection
from starlette.concurrency import run_in_threadpool

from ..domain.execution import ScheduleRecord
from . import config


@dataclass
class RequestContext:
    owner_id: str
    claims: dict
    connection: Connection | None = None
    records: dict[str, ScheduleRecord] = field(default_factory=dict)
    pending: dict[str, ScheduleRecord] = field(default_factory=dict)
    new_ids: set[str] = field(default_factory=set)
    finish: Callable[[bool], None] | None = None


current_request: ContextVar[RequestContext | None] = ContextVar('execution_request', default=None)


async def verified_user(authorization: str | None = Header(default=None)) -> RequestContext:
    if not config.SUPABASE_URL or not config.SUPABASE_PUBLISHABLE_KEY:
        raise HTTPException(503, 'Authentication is not configured')
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 8192:
        raise HTTPException(401, 'Bearer token required')
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f'{config.SUPABASE_URL}/auth/v1/user', headers={
                'apikey': config.SUPABASE_PUBLISHABLE_KEY, 'Authorization': f'Bearer {token}'
            })
        if response.status_code in (401, 403):
            raise HTTPException(401, 'Invalid session')
        response.raise_for_status()
        user = response.json()
        owner = str(uuid.UUID(user['id']))
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if not isinstance(claims, dict) or claims.get('sub') != owner or user.get('is_anonymous'):
            raise HTTPException(401, 'Invalid session')
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        raise HTTPException(503, 'Authentication service unavailable')
    return RequestContext(owner, claims)


async def execution_session(user: RequestContext = Depends(verified_user)):
    marker = current_request.set(user)
    succeeded = False
    try:
        yield user
        succeeded = True
    finally:
        try:
            if user.finish is not None:
                await run_in_threadpool(user.finish, succeeded)
        finally:
            current_request.reset(marker)


def require_recent_auth(user: RequestContext) -> None:
    # Token refresh changes iat; the AMR entry records the actual login instead.
    amr = user.claims.get('amr')
    now = time.time()
    valid = isinstance(amr, list) and any(
        isinstance(item, dict) and item.get('method') not in (None, 'anonymous', 'token_refresh')
        and isinstance(item.get('timestamp'), (int, float))
        and 0 <= now - item['timestamp'] <= 300
        for item in amr
    )
    if not valid:
        raise HTTPException(403, 'Recent sign-in required')
