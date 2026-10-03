"""Load intelligence provider boundary (Phase 3, §11, §26).

    LoadIntelligenceProvider
            |
    RuleBasedLoadIntelligence   <-- the default, always available
            |
    JevLoadIntelligence         <-- boundary only, no verified contract
    LLMClassifier               <-- future
    HybridClassifier            <-- future

WHY RULES FIRST. The backend must boot, classify and validate with no network,
no API key and no model. That buys four things this phase needs: deterministic
tests, an offline demo, low latency, and graceful degradation when an external
service is down. An LLM classifier can be added behind this Protocol later
without touching a single caller.

Jev is NOT INTEGRATED AND NOT FAKED (§26). `JevLoadIntelligence` exists so the
seam is visible and testable, but it refuses to invent a response: with no key
it reports `not_configured`, and with a key but no verified contract it reports
`not_integrated`. `get_load_intelligence()` therefore returns the rules-based
provider in Phase 3 regardless of configuration, because having a key is not the
same as having a contract.
"""

from __future__ import annotations

from typing import Protocol

from ..core import config
from .classification import Classification, RuleBasedLoadClassifier
from .load_normalizer import LoadIntelligenceService, LoadRequest
from ..domain.loads import LoadSpec


class IntelligenceUnavailable(RuntimeError):
    """The selected intelligence provider cannot serve this request."""


class IntelligenceNotConfigured(IntelligenceUnavailable):
    """Selected provider has no credentials."""


class IntelligenceNotIntegrated(IntelligenceUnavailable):
    """Credentials exist but no upstream contract is verified yet."""


class LoadIntelligenceProvider(Protocol):
    """What the API and the frontend depend on."""

    name: str

    def classify(self, text: str) -> Classification: ...

    def normalize(self, request: LoadRequest, now=None) -> LoadSpec: ...


class RuleBasedLoadIntelligence(LoadIntelligenceService):
    """The default. Deterministic, offline, dependency-free."""

    name = "rule_based"

    def __init__(self, classifier: RuleBasedLoadClassifier | None = None) -> None:
        super().__init__(classifier=classifier)


class JevLoadIntelligence:
    """Integration boundary for a Jev-based classifier.

    Deliberately refuses to guess. Phase 1 and 2 established no verified Jev API
    contract, so there is nothing to normalize and no response shape to parse.
    Rather than fabricate one, every call raises with a precise reason.
    """

    name = "jev"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def available(self) -> bool:
        """False until a verified contract exists. A key alone is not enough."""
        return False

    def _refuse(self):
        if not self.configured:
            raise IntelligenceNotConfigured(
                "Jev classifier selected but no API key is configured"
            )
        raise IntelligenceNotIntegrated(
            "Jev classification is not implemented yet — no verified API contract, so no "
            "response was fabricated"
        )

    def classify(self, text: str) -> Classification:
        self._refuse()
        raise AssertionError("unreachable")  # pragma: no cover

    def normalize(self, request: LoadRequest, now=None) -> LoadSpec:
        self._refuse()
        raise AssertionError("unreachable")  # pragma: no cover


def get_load_intelligence() -> LoadIntelligenceProvider:
    """The provider the API uses.

    Rules-based, always, in Phase 3. Switching to `LOAD_INTELLIGENCE_PROVIDER`
    would require a verified contract for that provider, which does not exist
    yet, so the setting is read but never honored for Jev.
    """
    requested = (config.LOAD_INTELLIGENCE_PROVIDER or "rule_based").lower()
    if requested == "jev":
        # Honored only if it were actually available; it never is in Phase 3.
        jev = JevLoadIntelligence(config.JEV_API_KEY)
        if jev.available():
            return jev  # pragma: no cover - unreachable until a contract lands
    return RuleBasedLoadIntelligence()
