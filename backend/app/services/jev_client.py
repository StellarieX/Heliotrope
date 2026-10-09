"""Client for Jev, TypeSafe AI's System One model (https://docs.typesafe.ai).

A System One model does not generate text. You send a `state` and a map of typed
`questions` (Choice, Score, Noul); it returns typed answers, each with calibrated
probabilities and a confidence. That is why it fits here: Heliotrope asks narrow
decisions ("what kind of load is a borewell pump?", "how essential is this
appliance?") and keeps the arithmetic and the constraints in its own code.

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <key>
    {"state": ..., "model": "jev-latest", "questions": {id: {type, instructions, criteria}}}

Behaviour this module guarantees:
  * A missing key, a network failure, a bad status or a malformed answer raises
    `JevError`; callers fall back to the built-in rules and say so. Nothing is invented.
  * 429 / 529 (documented as retryable) and transport errors get ONE retry.
  * Identical requests are served from a small cache. Only successes are cached.
  * A per-process cap on upstream calls per minute protects the key, because this
    API is public and unauthenticated.
  * After a transport failure, a 5xx, a rejected key or an exhausted cap, Jev is
    skipped for NEGATIVE_CACHE_S seconds and callers fall back immediately, so an
    outage costs one slow request instead of a slow request every time.
"""

from __future__ import annotations

import json
import threading
import math
import time
from collections import deque
from typing import Any

import httpx

from ..core import config

REQUEST_TIMEOUT_S = 8.0
RETRY_STATUSES = (429, 529)
RETRY_DELAY_S = 0.6
_CACHE_MAX = 512
NEGATIVE_CACHE_S = 30.0


class JevError(RuntimeError):
    """Jev could not give a trustworthy answer (the reason is in the message)."""


class JevNotConfigured(JevError):
    """No API key is set."""


_cache: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_call_times: deque[float] = deque()
_down_until: float = 0.0
_down_reason: str = ""


def resolve_api_key(explicit: str | None = None) -> str | None:
    """Explicit argument, else JEV_API_KEY, else TYPESAFE_API_KEY (the SDK's name)."""
    return explicit or config.JEV_API_KEY or config.TYPESAFE_API_KEY


def clear_state() -> None:
    """Reset the cache and the call window (tests)."""
    global _down_until, _down_reason
    with _lock:
        _cache.clear()
        _call_times.clear()
        _down_until = 0.0
        _down_reason = ""


def _mark_down(reason: str) -> None:
    global _down_until, _down_reason
    with _lock:
        _down_until = time.monotonic() + NEGATIVE_CACHE_S
        _down_reason = reason


def _down_message() -> str | None:
    with _lock:
        if time.monotonic() < _down_until:
            return _down_reason
    return None


def _within_rate_limit() -> bool:
    now = time.monotonic()
    with _lock:
        while _call_times and now - _call_times[0] > 60.0:
            _call_times.popleft()
        if len(_call_times) >= max(1, config.JEV_MAX_CALLS_PER_MIN):
            return False
        _call_times.append(now)
        return True


def ask(
    state: Any,
    questions: dict[str, dict[str, Any]],
    api_key: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate `state` against `questions`; returns the `answers` map.

    Every question id in the request must come back with the matching answer type,
    otherwise the whole response is rejected rather than half-trusted.
    """
    key = resolve_api_key(api_key)
    if not key:
        raise JevNotConfigured("no Jev API key configured")

    body = {"state": state, "model": config.JEV_MODEL, "questions": questions}
    cache_key = json.dumps(body, sort_keys=True, default=str)
    with _lock:
        hit = _cache.get(cache_key)
    if hit is not None:
        return hit

    down = _down_message()
    if down is not None:
        raise JevError(f"{down} (skipping Jev for a short while)")

    if not _within_rate_limit():
        _mark_down("Jev call limit reached")
        raise JevError("Jev call limit reached; try again in a minute")

    url = f"{config.JEV_BASE_URL}/v1/systemone"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload: dict[str, Any] | None = None
    last: str = ""
    for attempt in range(2):
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_S)
        except httpx.RequestError as exc:
            last = f"network error: {exc}"
        else:
            if resp.status_code in RETRY_STATUSES:
                last = f"Jev is busy (HTTP {resp.status_code})"
            elif resp.status_code == 401:
                _mark_down("Jev rejected the API key (HTTP 401)")
                raise JevError("Jev rejected the API key (HTTP 401)")
            elif resp.status_code == 422:
                raise JevError(f"Jev rejected the request (HTTP 422): {resp.text[:200]}")
            else:
                try:
                    resp.raise_for_status()
                    payload = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    if resp.status_code >= 500:
                        _mark_down(f"Jev server error (HTTP {resp.status_code})")
                    raise JevError(f"Jev request failed: {exc}") from exc
                break
        if attempt == 0:
            time.sleep(RETRY_DELAY_S)
    if payload is None:
        _mark_down(last or "Jev did not answer")
        raise JevError(last or "Jev did not answer")

    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise JevError("Jev response has no 'answers' map")
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict) or a.get("type") != q.get("type"):
            raise JevError(f"Jev answer for {qid!r} is missing or of the wrong type")

    with _lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[cache_key] = answers
    return answers


def _unit(x: Any) -> bool:
    """True for a finite real number in [0, 1] (confidences and probabilities)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 0.0 <= x <= 1.0


def choice(answer: dict[str, Any], allowed: set[str]) -> tuple[str, float, dict[str, float]]:
    """(chosen option, confidence, probabilities) from a Choice answer, validated."""
    pick = answer.get("choice")
    conf = answer.get("confidence")
    probs = answer.get("probabilities")
    if not isinstance(pick, str) or pick not in allowed or not _unit(conf) or not isinstance(probs, dict):
        raise JevError(f"unexpected Choice answer: {str(answer)[:120]}")
    clean = {str(k): float(v) for k, v in probs.items() if _unit(v)}
    return pick, float(conf), clean


def score(answer: dict[str, Any], levels: int) -> tuple[float, float]:
    """(score on 0..levels-1, confidence) from a Score answer, validated."""
    val = answer.get("score")
    conf = answer.get("confidence")
    if (
        not isinstance(val, (int, float))
        or isinstance(val, bool)
        or not math.isfinite(val)
        or not _unit(conf)
        or not (-0.001 <= val <= levels - 1 + 0.001)
    ):
        raise JevError(f"unexpected Score answer: {str(answer)[:120]}")
    return float(val), float(conf)
