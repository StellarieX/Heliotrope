"""Backend configuration from environment. External keys are optional."""

import os


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


HELIOTROPE_ENV = _get("HELIOTROPE_ENV", "development")
PORT = int(_get("PORT", "8000"))
LOG_LEVEL = _get("LOG_LEVEL", "info")

# Optional: never required to boot, never sent to the browser.
JEV_API_KEY = os.environ.get("JEV_API_KEY")
ELECTRICITY_MAPS_API_KEY = os.environ.get("ELECTRICITY_MAPS_API_KEY")
