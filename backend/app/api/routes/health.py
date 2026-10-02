"""GET /api/v1/health — real liveness response."""

from fastapi import APIRouter

from ...core import config

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "heliotrope-backend", "env": config.HELIOTROPE_ENV}
