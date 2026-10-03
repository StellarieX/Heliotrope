"""External carbon-intensity adapter boundary.

No vendor contract is verified in this phase, so this adapter intentionally
does NOT call anything: it is the normalization seam a real integration
(Electricity Maps or similar) will plug into in a later phase.

States are honest: unconfigured (no key) vs. not-yet-integrated (key
present, no verified contract). Credentials always stay server-side.
"""

from datetime import datetime

from ...domain.carbon import CarbonPoint


class ProviderNotConfigured(RuntimeError):
    """External provider selected but credentials are absent."""


class ProviderNotIntegrated(RuntimeError):
    """Credentials exist but no external API contract is verified yet."""


class ExternalProvider:
    name = "external"

    def __init__(self, api_key: str | None):
        self._api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def get_signal(self, start: datetime, end: datetime, resolution_minutes: int) -> list[CarbonPoint]:
        if not self.configured:
            raise ProviderNotConfigured(
                "external carbon provider selected but no API key is configured"
            )
        raise ProviderNotIntegrated(
            "external carbon integration is not implemented yet — no vendor contract verified"
        )

    @staticmethod
    def normalize(records: list[dict]) -> list[CarbonPoint]:
        """Vendor records -> canonical points. Implemented per-vendor later."""
        raise ProviderNotIntegrated("no vendor response format to normalize yet")
