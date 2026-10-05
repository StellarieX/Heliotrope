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


def test_requesting_jev_with_key_returns_jev_that_falls_back_per_call(monkeypatch):
    """With a key, "jev" routes to the Jev provider; without network it falls
    back per call, so the user still gets a rule-based-quality answer."""
    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", boom)
    monkeypatch.setattr("app.services.load_intelligence.config.JEV_API_KEY", "secret-key")
    monkeypatch.setattr(
        "app.services.load_intelligence.config.LOAD_INTELLIGENCE_PROVIDER", "jev"
    )
    provider = get_load_intelligence()
    assert isinstance(provider, JevLoadIntelligence)
    assert provider.name == "jev"
    assert provider.classify("EV").job_type is LoadType.DEFERRABLE_INTERRUPTIBLE


def test_requesting_jev_without_key_returns_rules_based(monkeypatch):
    """Unconfigured: the default is preserved, no exception, no network."""
    monkeypatch.setattr("app.services.load_intelligence.config.JEV_API_KEY", None)
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


# --- Jev falls back honestly, never fabricates (§26, M4) ---------------------


class _JevResponse:
    """What api.typesafe.ai returns, per docs.typesafe.ai/api."""

    status_code = 200

    def __init__(self, job_type="DEFERRABLE_ATOMIC", category="Laundry", jt_conf=0.9, cat_conf=0.85, jt_probs=None, cat_probs=None):
        self._body = {
            "model": "jev-1.13.0",
            "answers": {
                "job_type": {
                    "type": "choice", "choice": job_type, "confidence": jt_conf,
                    "probabilities": jt_probs or {job_type: 0.95, "FIXED": 0.05},
                },
                "category": {
                    "type": "choice", "choice": category, "confidence": cat_conf,
                    "probabilities": cat_probs or {category: 0.9, "Unknown": 0.1},
                },
            },
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _fresh_jev_state():
    from app.services import jev_client

    jev_client.clear_state()
    yield
    jev_client.clear_state()


def test_jev_without_a_key_falls_back_to_rules():
    """No key -> rule-based fallback with the fallback recorded, never an error."""
    result = JevLoadIntelligence(None).classify("EV")
    assert result.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE
    assert any(
        a.field == "classifier" and "rule-based fallback" in a.detail
        for a in result.assumptions
    )


def test_jev_with_http_error_falls_back_to_rules(monkeypatch):
    """A dead upstream is a fallback, not a user-visible error (no network)."""
    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", boom)
    monkeypatch.setattr("app.services.jev_client.RETRY_DELAY_S", 0)
    result = JevLoadIntelligence("secret").classify("EV")
    assert result.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE
    assert any(a.field == "classifier" for a in result.assumptions)


def test_jev_with_malformed_answer_falls_back_to_rules(monkeypatch):
    import httpx

    class _Bad(_JevResponse):
        def json(self):
            return {"answers": {"job_type": {"type": "noul", "noul": 0.5}}}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Bad())
    result = JevLoadIntelligence("secret").classify("washing machine")
    assert result.matched_rule == "washing_machine"


def test_jev_with_unknown_option_falls_back_to_rules(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _JevResponse(job_type="WARP"))
    result = JevLoadIntelligence("secret").classify("EV")
    assert result.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE


def test_jev_success_uses_jev_labels_and_calibrated_confidence(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: _JevResponse(jt_conf=0.9, cat_conf=0.85, jt_probs={"DEFERRABLE_ATOMIC": 0.8, "FIXED": 0.2}),
    )
    result = JevLoadIntelligence("secret").classify("laundry")
    assert result.matched_rule == "jev"
    assert result.job_type is LoadType.DEFERRABLE_ATOMIC
    # The weaker of the two Choice confidences, not a constant.
    assert result.confidence == pytest.approx(0.85)
    assert result.ambiguous is False
    assert any(a.job_type is LoadType.FIXED and a.weight == pytest.approx(0.2) for a in result.alternatives)
    assert any(a.origin.value == "estimated" for a in result.assumptions)


def test_low_jev_confidence_is_flagged_ambiguous(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _JevResponse(jt_conf=0.4, cat_conf=0.9))
    result = JevLoadIntelligence("secret").classify("mystery box")
    assert result.confidence == pytest.approx(0.4)
    assert result.ambiguous is True


def test_jev_available_means_key_present():
    """available() is True when a key is configured, False without one."""
    assert JevLoadIntelligence(None).available() is False
    assert JevLoadIntelligence("secret").available() is True


def test_jev_normalize_falls_back_offline():
    spec = JevLoadIntelligence(None).normalize(LoadRequest(name="EV"))
    assert spec.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE


def test_the_app_never_breaks_when_jev_is_absent():
    """The whole point of §26: a missing Jev must not be a user-visible error."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    for name in ("EV", "washing machine", "geyser", "fan"):
        assert client.post("/api/v1/loads/classify", json={"name": name}).status_code == 200
