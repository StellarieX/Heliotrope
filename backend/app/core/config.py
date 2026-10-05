"""Backend configuration from environment. External keys are optional."""

import os


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _get_int(name: str, default: str) -> int:
    raw = _get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}={raw!r}: expected an integer")


HELIOTROPE_ENV = _get("HELIOTROPE_ENV", "development")
PORT = _get_int("PORT", "8000")
LOG_LEVEL = _get("LOG_LEVEL", "info")

def _parse_origins(raw: str) -> list[str]:
    return [
        origin.strip().rstrip("/")
        for origin in raw.split(",")
        if origin.strip()
    ]


# Browser origins allowed to call this API. The frontend (Next.js) runs on a
# different port than the backend, so without this every fetch() from the page
# is blocked and the UI silently reports "backend unreachable" even when it is
# running. In development any loopback port is allowed because the dev server
# picks a free one; in production the list must be explicit and defaults to
# nothing, i.e. same-origin only.
#
# Vercel wiring: when the backend itself runs on Vercel (or behind a Vercel
# frontend that calls it cross-origin), VERCEL_URL / VERCEL_PROJECT_PRODUCTION_URL
# are set automatically by the platform. They are trusted here because they are
# this deployment's own canonical hosts, so CORS works without manually copying
# per-deployment URLs into CORS_ALLOW_ORIGINS. An explicit CORS_ALLOW_ORIGINS
# entry always wins; set that variable in the Vercel dashboard for any
# additional (preview, custom-domain) origins.
_VERCEL_HOSTS = [
    host.strip().rstrip("/")
    for host in (
        _get("VERCEL_URL", ""),
        _get("VERCEL_PROJECT_PRODUCTION_URL", ""),
    )
    if host.strip()
]
CORS_ALLOW_ORIGINS = _parse_origins(_get("CORS_ALLOW_ORIGINS", ""))
for _vercel_host in _VERCEL_HOSTS:
    _candidate = _vercel_host if "://" in _vercel_host else f"https://{_vercel_host}"
    if _candidate not in CORS_ALLOW_ORIGINS:
        CORS_ALLOW_ORIGINS.append(_candidate)
del _VERCEL_HOSTS
CORS_ALLOW_LOCALHOST_IN_DEVELOPMENT = HELIOTROPE_ENV != "production"

# Optional: never required to boot, never sent to the browser.
# Precedence for the Gemini/Jev classifier key: GEMINI_API_KEY wins when set;
# JEV_API_KEY is honored as a legacy alias so existing deployments keep working.
# Resolve via `GEMINI_API_KEY or JEV_API_KEY` at the use site (see
# services/load_intelligence.py) rather than baking the fallback in here, so
# tests and callers can distinguish "new key set" from "legacy alias set".
JEV_API_KEY = os.environ.get("JEV_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ELECTRICITY_MAPS_API_KEY = os.environ.get("ELECTRICITY_MAPS_API_KEY")
ELECTRICITY_MAPS_ZONE = _get("ELECTRICITY_MAPS_ZONE", "US-CAL-CISO")

# Carbon Intelligence (Phase 2)
CARBON_PROVIDER = _get("CARBON_PROVIDER", "synthetic")
CARBON_CSV_PATH = _get("CARBON_CSV_PATH", "")
CARBON_MAX_RANGE_DAYS = _get_int("CARBON_MAX_RANGE_DAYS", "7")
CARBON_CACHE_TTL_S = _get_int("CARBON_CACHE_TTL_S", "300")
CARBON_SYNTHETIC_SEED = _get_int("CARBON_SYNTHETIC_SEED", "7")

# Load Intelligence (Phase 3). The default must stay "rule_based": no external
# classifier is required for the backend to work, and no external response is
# ever fabricated. "jev" is honored only once a verified contract exists.
LOAD_INTELLIGENCE_PROVIDER = _get("LOAD_INTELLIGENCE_PROVIDER", "rule_based")
DEFAULT_LOAD_TIMEZONE = _get("DEFAULT_LOAD_TIMEZONE", "UTC")
