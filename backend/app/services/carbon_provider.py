"""Carbon provider protocol + error vocabulary (Phase 1 contract, kept).

Working providers live in services/providers/; selection lives in
services/carbon_service.py. This module keeps the structural Protocol so
Phase-1-style contract code still type-checks.
"""

from datetime import datetime
from enum import Enum
from typing import Protocol


class ProviderNotAvailable(RuntimeError):
    """Raised when a carbon provider is requested before it exists."""


class ProviderName(str, Enum):
    SYNTHETIC = "synthetic"
    CSV = "csv"
    EXTERNAL = "external"


class CarbonProvider(Protocol):
    name: str

    def get_signal(self, start: datetime, end: datetime, resolution_minutes: int): ...
