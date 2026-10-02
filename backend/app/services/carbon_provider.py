"""Carbon provider abstraction.

Phase 1 establishes the interface only. Providers raise
ProviderNotAvailable: the contract exists, live/synthetic signal
implementations land in Phase 2.
"""

from datetime import datetime
from enum import Enum
from typing import Protocol

from ..domain.carbon import CarbonSignal


class ProviderNotAvailable(RuntimeError):
    """Raised when a carbon provider is requested before it exists."""


class ProviderName(str, Enum):
    SYNTHETIC = "synthetic"
    CSV = "csv"
    EXTERNAL = "external"


class CarbonProvider(Protocol):
    name: ProviderName

    def get_signal(self, start: datetime, end: datetime, resolution_minutes: int) -> CarbonSignal: ...


class _UnimplementedProvider:
    name: ProviderName = ProviderName.SYNTHETIC

    def get_signal(self, start: datetime, end: datetime, resolution_minutes: int) -> CarbonSignal:
        raise ProviderNotAvailable(
            f"{self.name} carbon provider is not implemented yet (Phase 2). No signal was produced."
        )


class SyntheticDuckCurveProvider(_UnimplementedProvider):
    name = ProviderName.SYNTHETIC


class CSVProvider(_UnimplementedProvider):
    name = ProviderName.CSV


class ExternalProvider(_UnimplementedProvider):
    name = ProviderName.EXTERNAL


PROVIDERS: dict[ProviderName, CarbonProvider] = {
    ProviderName.SYNTHETIC: SyntheticDuckCurveProvider(),
    ProviderName.CSV: CSVProvider(),
    ProviderName.EXTERNAL: ExternalProvider(),
}


def get_provider(name: ProviderName) -> CarbonProvider:
    return PROVIDERS[name]
