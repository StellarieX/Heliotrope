"""Heliotrope backend — Phase 1 contracts, Phase 2 carbon, Phase 3 load intelligence.

No solver engine yet: POST /api/v1/schedule still returns 501 because the
optimizer belongs to Phase 4. Phase 3 adds classification and feasibility,
which are pure physical reasoning and are live.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .api.routes import carbon, forecast, health, loads, schedule
from .core import config

app = FastAPI(title="Heliotrope Backend", version="0.1.0")

# The frontend runs on a different origin (Next.js on :3000, this API on :8000).
# Without CORS every browser fetch() fails while curl keeps working, which makes
# the UI report "backend unreachable" for a backend that is perfectly healthy.
_cors_kwargs = {
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}
if config.CORS_ALLOW_LOCALHOST_IN_DEVELOPMENT:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        **_cors_kwargs,
    )
if config.CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ALLOW_ORIGINS,
        **_cors_kwargs,
    )

@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """Give schema rejections the same error vocabulary as the routes do.

    Phase 5 added several endpoints whose invalid input is rejected by a model
    validator rather than by a route (a partial-slot deadline buffer, a
    coverage outside (0, 1)). Without this handler FastAPI answers those with a
    bare pydantic error list and no `code`, so a client would have to special-case
    which endpoints use which shape. `detail` is preserved exactly as FastAPI
    produced it, so nothing existing changes.
    """
    return JSONResponse(
        status_code=422,
        content={
            # jsonable_encoder is required, not cosmetic: a pydantic error's
            # `ctx` can hold the raw ValueError that caused it, which is not
            # JSON serializable and turns this 422 into a 500.
            "detail": jsonable_encoder(exc.errors()),
            "code": "invalid_request",
            "message": "the request body did not validate",
        },
    )


app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(schedule.router, prefix="/api/v1", tags=["schedule"])
app.include_router(carbon.router, prefix="/api/v1", tags=["carbon"])
app.include_router(forecast.router, prefix="/api/v1", tags=["forecast"])
app.include_router(loads.router, prefix="/api/v1", tags=["loads"])


@app.get("/")
def root() -> dict:
    return {"service": "heliotrope-backend", "docs": "/docs"}
