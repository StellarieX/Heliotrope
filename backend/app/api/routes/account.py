"""Delete the signed-in account after a recent verified sign-in."""
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from ...core import config
from ...core.auth import RequestContext, require_recent_auth, verified_user

router = APIRouter()


@router.delete('/account', status_code=204)
async def delete_account(user: RequestContext = Depends(verified_user)) -> Response:
    require_recent_auth(user)
    if not config.SUPABASE_SECRET_KEY or not config.DATABASE_URL:
        raise HTTPException(503, 'Account deletion is not configured')
    try:
        session_id = str(uuid.UUID(user.claims['session_id']))
        import psycopg
        with psycopg.connect(config.DATABASE_URL, prepare_threshold=None) as conn:
            session = conn.execute(
                'SELECT 1 FROM auth.sessions WHERE id = %s AND user_id = %s',
                (session_id, user.owner_id),
            ).fetchone()
        if not session:
            raise HTTPException(401, 'Session is no longer active')
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.delete(
                f'{config.SUPABASE_URL}/auth/v1/admin/users/{user.owner_id}',
                headers={'apikey': config.SUPABASE_SECRET_KEY,
                         'Authorization': f'Bearer {config.SUPABASE_SECRET_KEY}'},
            )
            response.raise_for_status()
    except HTTPException:
        raise
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, 'Invalid session')
    except (httpx.HTTPError, psycopg.Error):
        raise HTTPException(503, 'Account deletion unavailable')
    return Response(status_code=204)
