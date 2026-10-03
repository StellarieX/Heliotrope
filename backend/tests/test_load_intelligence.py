"""Provider boundary: rules-first default, no fabricated Jev responses (§11, §26).

The point of these tests is negative: they exist to prove the backend does NOT
need an external service, and that the Jev seam refuses rather than invents.
"""

import pytest

from app.domain.loads import LoadType
from app.services.classification import Classification
from app.services.load_intelligence import (
    IntelligenceNotConfigured,
    IntelligenceNotIntegrated,
    JevLoadIntelligence,
    RuleBasedLoadIntelligence,
    get_load_intelligence,
)
from app.services.load_normalizer import LoadRequest


# --- the default needs nothing external (§11) ------------------------------


def test_default_provider_is_rule_based():
    provider = get_load_intelligence()
    assert provider.name == "rule_based"
    assert isinstance(provider, RuleBasedLoadIntelligence)


def test_default_provider_works_with_no_api_key(monkeypatch):
    monkeypatch.setattr("app.services.load_intelligence.config.JEV_API_KEY", None)
    monkeypatch.setattr(
        "app.services.load_intelligence.config.LOAD_INTELLIGENCE_PROVIDER", "rule_based"
    )
    provider = get_load_intelligence()
    assert provider.classify("EV").job_type is LoadType.DEFERRABLE_INTERRUPTIBLE


def test_requesting_jev_still_returns_the_rules_based_provider(monkeypatch):
    """Having a key is not the same as having a verified contract, so Phase 3
    never routes to Jev — and never fails because it did not."""
    monkeypatch.setattr("app.services.load_intelligence.config.JEV_API_KEY", "secret-key")
    monkeypatch.setattr(
        "app.services.load_intelligence.config.LOAD_INTELLIGENCE_PROVIDER", "jev"
    )
    provider = get_load_intelligence()
    assert isinstance(provider, RuleBasedLoadIntelligence)
    assert provider.name == "rule_based"


def test_unknown_provider_name_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(
        "app.services.load_intelligence.config.LOAD_INTELLIGENCE_PROVIDER", "crystal-ball"
    )
    assert isinstance(get_load_intelligence(), RuleBasedLoadIntelligence)


def test_provider_satisfies_the_protocol_shape():
    provider = get_load_intelligence()
    assert isinstance(provider.classify("EV"), Classification)
    spec = provider.normalize(LoadRequest(name="EV", release_wall="21:00", deadline_wall="07:00"))
    assert spec.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE
    assert spec.window_minutes() == pytest.approx(600.0)


# --- Jev refuses rather than invents (§26) ---------------------------------


def test_jev_without_a_key_is_unconfigured():
    with pytest.raises(IntelligenceNotConfigured, match="no API key"):
        JevLoadIntelligence(None).classify("EV")


def test_jev_with_a_key_is_not_integrated():
    with pytest.raises(IntelligenceNotIntegrated, match="no verified API contract"):
        JevLoadIntelligence("secret").classify("EV")


def test_jev_never_reports_itself_available():
    """Until a contract exists, availability is False with or without a key."""
    assert JevLoadIntelligence(None).available() is False
    assert JevLoadIntelligence("secret").available() is False


def test_jev_normalize_also_refuses():
    with pytest.raises(IntelligenceNotIntegrated):
        JevLoadIntelligence("secret").normalize(LoadRequest(name="EV"))


def test_the_app_never_breaks_when_jev_is_absent():
    """The whole point of §26: a missing Jev must not be a user-visible error."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    for name in ("EV", "washing machine", "geyser", "fan"):
        assert client.post("/api/v1/loads/classify", json={"name": name}).status_code == 200
