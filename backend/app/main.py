"""Heliotrope backend — Phase 1: contracts only, no solver engine."""

from fastapi import FastAPI

from .api.routes import carbon, health, schedule

app = FastAPI(title="Heliotrope Backend", version="0.1.0")

app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(schedule.router, prefix="/api/v1", tags=["schedule"])
app.include_router(carbon.router, prefix="/api/v1", tags=["carbon"])


@app.get("/")
def root() -> dict:
    return {"service": "heliotrope-backend", "docs": "/docs"}
