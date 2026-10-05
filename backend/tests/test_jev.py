"""Jev (TypeSafe AI System One): client, classification, prioritization.

No network: the upstream is replaced by canned responses in the documented format
(docs.typesafe.ai/api).
"""

import httpx
import pytest

from app.core import config
from app.services import jev_client, load_intelligence, prioritization
from app.services.jev_client import JevError, JevNotConfigured
from app.services.prioritization import PriorityLoad, prioritize


class _Resp:
    def __init__(self, body=None, status=200, text=""):
        self._body = body
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)  # type: ignore[arg-type]

    def json(self):
        return self._body


def _answers(**kw):
    return {"model": "jev-1.13.0", "answers": kw, "usage": {"input_tokens": 1, "output_tokens": 1}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    jev_client.clear_state()
    monkeypatch.setattr(jev_client, "RETRY_DELAY_S", 0)
    monkeypatch.setattr(config, "JEV_API_KEY", None)
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", None)
    yield
    jev_client.clear_state()


Q = {"q": {"type": "noul", "instructions": "Is it?"}}


# ---- client ---------------------------------------------------------------------

def test_request_matches_the_documented_api(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return _Resp(_answers(q={"type": "noul", "noul": 0.9}))

    monkeypatch.setattr(httpx, "post", fake_post)
    out = jev_client.ask({"x": 1}, Q, api_key="K")
    assert out["q"]["noul"] == 0.9
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["headers"]["Authorization"] == "Bearer K"
    assert seen["body"] == {"state": {"x": 1}, "model": "jev-latest", "questions": Q}


def test_no_key_is_refused_without_a_request(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not call out"))
    with pytest.raises(JevNotConfigured):
        jev_client.ask("s", Q)


def test_both_key_names_work_and_jev_key_wins(monkeypatch):
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "sdk")
    assert jev_client.resolve_api_key() == "sdk"
    monkeypatch.setattr(config, "JEV_API_KEY", "jev")
    assert jev_client.resolve_api_key() == "jev"


def test_the_key_is_only_ever_sent_to_typesafe(monkeypatch):
    urls = []

    def fake_post(url, **k):
        urls.append(url)
        return _Resp(_answers(q={"type": "noul", "noul": 0.1}))

    monkeypatch.setattr(httpx, "post", fake_post)
    jev_client.ask("s", Q, api_key="secret")
    assert urls and all(u.startswith("https://api.typesafe.ai/") for u in urls)
    assert not any("google" in u for u in urls)


def test_unauthorized_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp(status=401)

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(JevError, match="401"):
        jev_client.ask("s", Q, api_key="K")
    assert calls["n"] == 1


def test_busy_responses_get_one_retry(monkeypatch):
    seq = iter([_Resp(status=529), _Resp(_answers(q={"type": "noul", "noul": 0.2}))])
    monkeypatch.setattr(httpx, "post", lambda *a, **k: next(seq))
    assert jev_client.ask("s", Q, api_key="K")["q"]["noul"] == 0.2


def test_answer_type_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(_answers(q={"type": "score", "score": 1, "confidence": 1})))
    with pytest.raises(JevError):
        jev_client.ask("s", Q, api_key="K")


def test_identical_requests_hit_the_cache_but_failures_do_not(monkeypatch):
    calls = {"n": 0}

    def ok(*a, **k):
        calls["n"] += 1
        return _Resp(_answers(q={"type": "noul", "noul": 0.5}))

    monkeypatch.setattr(httpx, "post", ok)
    jev_client.ask("s", Q, api_key="K")
    jev_client.ask("s", Q, api_key="K")
    assert calls["n"] == 1

    jev_client.clear_state()
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(status=500))
    for _ in range(2):
        with pytest.raises(JevError):
            jev_client.ask("s", Q, api_key="K")


def test_call_cap_protects_the_key(monkeypatch):
    monkeypatch.setattr(config, "JEV_MAX_CALLS_PER_MIN", 2)
    calls = {"n": 0}

    def ok(*a, **k):
        calls["n"] += 1
        return _Resp(_answers(q={"type": "noul", "noul": 0.5}))

    monkeypatch.setattr(httpx, "post", ok)
    jev_client.ask("a", Q, api_key="K")
    jev_client.ask("b", Q, api_key="K")
    with pytest.raises(JevError, match="limit"):
        jev_client.ask("c", Q, api_key="K")
    assert calls["n"] == 2


# ---- provider selection -----------------------------------------------------------

def test_auto_uses_jev_only_when_a_key_exists(monkeypatch):
    monkeypatch.setattr(config, "LOAD_INTELLIGENCE_PROVIDER", "auto")
    assert load_intelligence.get_load_intelligence().name == "rule_based"
    monkeypatch.setattr(config, "JEV_API_KEY", "k")
    assert load_intelligence.get_load_intelligence().name == "jev"


def test_rule_based_never_calls_jev_even_with_a_key(monkeypatch):
    monkeypatch.setattr(config, "LOAD_INTELLIGENCE_PROVIDER", "rule_based")
    monkeypatch.setattr(config, "JEV_API_KEY", "k")
    assert load_intelligence.get_load_intelligence().name == "rule_based"


def test_a_jev_key_can_not_reach_any_other_provider():
    """Regression: the key once doubled as a Gemini key, which would have sent a
    TypeSafe key to Google. There is no Gemini code path left."""
    import app.services.load_intelligence as li

    src = open(li.__file__, encoding="utf-8").read().lower()
    assert "googleapis" not in src and "gemini" not in src


# ---- prioritization -----------------------------------------------------------------

def _score_answer(score, conf):
    return _Resp(_answers(importance={
        "type": "score", "score": score, "confidence": conf,
        "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
        "probabilities": {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9},
    }))


LOADS = [
    PriorityLoad(id="pump", name="Borewell pump", kind="Pumping", power_kw=1.5, hours_until_ready=6, flex_hours=1),
    PriorityLoad(id="lamp", name="Decor lights", kind="Always-on", power_kw=0.1, hours_until_ready=6, flex_hours=1),
]


def test_without_a_key_ranking_is_the_labelled_heuristic():
    r = prioritize(LOADS)
    assert r.provider == "heuristic" and all(i.source == "heuristic" for i in r.items)
    assert any("not configured" in n for n in r.notes)


def test_jev_importance_changes_the_order_of_otherwise_equal_loads(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        name = json["state"]["appliance"]
        return _score_answer(3.0 if name == "Borewell pump" else 0.0, 0.9)

    monkeypatch.setattr(httpx, "post", fake_post)
    r = prioritize(
        [LOADS[1].model_copy(update={"power_kw": 1.5}), LOADS[0]], api_key="K"
    )
    assert r.provider == "jev"
    assert [i.id for i in r.items] == ["pump", "lamp"]
    top = r.items[0]
    assert top.source == "jev" and top.importance_label == "essential" and top.importance_confidence == 0.9
    assert "Jev: essential" in top.reason


def test_low_confidence_importance_is_not_trusted(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _score_answer(3.0, 0.2))
    r = prioritize(LOADS, api_key="K")
    assert r.provider == "heuristic"
    assert all(i.source == "heuristic" and "unsure" in i.reason for i in r.items)


def test_one_failed_load_does_not_fail_the_ranking(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        if json["state"]["appliance"] == "Decor lights":
            return _Resp(status=500)
        return _score_answer(2.0, 0.9)

    monkeypatch.setattr(httpx, "post", fake_post)
    r = prioritize(LOADS, api_key="K")
    by = {i.id: i for i in r.items}
    assert by["pump"].source == "jev" and by["lamp"].source == "heuristic"
    assert r.provider == "jev" and r.notes


def test_weights_sum_to_one():
    assert sum(prioritization.W_WITH_IMPORTANCE.values()) == pytest.approx(1.0)
    assert sum(prioritization.W_HEURISTIC.values()) == pytest.approx(1.0)


def test_prioritize_endpoint_validates_and_answers(client):
    ok = client.post("/api/v1/loads/prioritize", json={"loads": [
        {"id": "a", "name": "EV", "power_kw": 7.4, "hours_until_ready": 8, "flex_hours": 3}]})
    assert ok.status_code == 200 and ok.json()["items"][0]["id"] == "a"
    assert client.post("/api/v1/loads/prioritize", json={"loads": []}).status_code == 422
    too_many = [{"id": str(i), "name": "x", "power_kw": 1, "hours_until_ready": 1} for i in range(26)]
    assert client.post("/api/v1/loads/prioritize", json={"loads": too_many}).status_code == 422
    assert client.post("/api/v1/loads/prioritize", json={"loads": [
        {"id": "a", "name": "EV", "power_kw": -1, "hours_until_ready": 1}]}).status_code == 422
