"""Load prioritization: which load matters first.

Prioritization is NOT optimization. It orders a household's loads by how much each
one needs attention; it does not decide start times (POST /schedule does).

The score is a composite, the pattern TypeSafe recommends for System One models:
break a judgement into atomic parts and combine them with weights that stay in code.

    time pressure  (code)  how soon the load must be ready
    size           (code)  how much power it draws
    rigidity       (code)  how little flexibility the user allowed
    importance     (Jev)   how essential the appliance is to the household

Only importance needs semantic understanding ("a water pump" vs "a decorative
light"), so only that is asked of Jev, as one Score question with an explicit
rubric. A low-confidence answer is not trusted: below JEV_MIN_CONFIDENCE the load
is scored without importance, and the item says so. If Jev is unconfigured or
fails, every load is scored by the heuristic and the response is labelled
`heuristic`, so a ranking is never presented as AI when it is not.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import jev_client
from .jev_client import JevError

JEV_MIN_CONFIDENCE = 0.5
MAX_LOADS = 25

# Weights when Jev's importance is available / not available. Each set sums to 1.
W_WITH_IMPORTANCE = {"urgency": 0.35, "importance": 0.25, "size": 0.20, "rigidity": 0.20}
W_HEURISTIC = {"urgency": 0.45, "size": 0.30, "rigidity": 0.25}

_IMPORTANCE_LEVELS = [
    "Optional comfort or convenience: skipping it today would be harmless.",
    "Useful: missing it is a nuisance but easy to recover from.",
    "Important: missing it disrupts the day (transport, hygiene, cooking, studying).",
    "Essential: missing it causes real harm or loss (health, food safety, water supply, income).",
]
_IMPORTANCE_LABELS = ["optional", "useful", "important", "essential"]


class PriorityLoad(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    kind: Optional[str] = Field(default=None, max_length=60)
    power_kw: float = Field(ge=0, le=1000)
    hours_until_ready: float = Field(ge=0, le=168)
    flex_hours: float = Field(default=0, ge=0, le=24)


class PriorityRequest(BaseModel):
    loads: list[PriorityLoad] = Field(min_length=1, max_length=MAX_LOADS)


class PriorityItem(BaseModel):
    id: str
    score: int = Field(ge=0, le=100)
    band: Literal["Critical", "High", "Normal", "Low"]
    reason: str
    source: Literal["jev", "heuristic"]
    importance: Optional[float] = None
    importance_label: Optional[str] = None
    importance_confidence: Optional[float] = None


class PriorityResponse(BaseModel):
    provider: Literal["jev", "heuristic"]
    items: list[PriorityItem]
    notes: list[str] = Field(default_factory=list)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _band(score: int) -> str:
    return "Critical" if score >= 70 else "High" if score >= 50 else "Normal" if score >= 30 else "Low"


def _ask_importance(load: PriorityLoad, api_key: str | None) -> tuple[float, float]:
    state = {
        "appliance": load.name,
        "category": load.kind or "unspecified",
        "power_kw": load.power_kw,
        "must_be_ready_in_hours": round(load.hours_until_ready, 1),
    }
    answers = jev_client.ask(
        state,
        {
            "importance": {
                "type": "score",
                "instructions": "How essential is it to the household that this appliance finishes its job on time?",
                "criteria": _IMPORTANCE_LEVELS,
            }
        },
        api_key=api_key,
    )
    return jev_client.score(answers["importance"], len(_IMPORTANCE_LEVELS))


def _score_one(load: PriorityLoad, importance: Optional[tuple[float, float]]) -> PriorityItem:
    urgency = _clamp(1 - load.hours_until_ready / 24)
    size = _clamp(load.power_kw / 7.5)
    rigidity = 1 - _clamp(load.flex_hours / 6)

    trusted = importance is not None and importance[1] >= JEV_MIN_CONFIDENCE
    if trusted:
        imp = importance[0] / (len(_IMPORTANCE_LEVELS) - 1)  # type: ignore[index]
        w = W_WITH_IMPORTANCE
        raw = w["urgency"] * urgency + w["importance"] * imp + w["size"] * size + w["rigidity"] * rigidity
    else:
        w = W_HEURISTIC
        raw = w["urgency"] * urgency + w["size"] * size + w["rigidity"] * rigidity
    score = int(round(100 * raw))
    band = _band(score)

    when = "needed right now" if load.hours_until_ready < 0.1 else f"ready in {load.hours_until_ready:.1f}h"
    flex = f"+{load.flex_hours:g}h flexible"
    verdict = "schedule first." if band in ("Critical", "High") else "fits around the big ones."
    if trusted:
        label = _IMPORTANCE_LABELS[min(3, max(0, round(importance[0])))]  # type: ignore[index]
        why = f"{when} · {load.power_kw:g} kW · {flex} · Jev: {label} ({importance[1]:.0%} sure) — {verdict}"  # type: ignore[index]
        return PriorityItem(
            id=load.id, score=score, band=band, reason=why, source="jev",
            importance=round(importance[0], 2), importance_label=label,  # type: ignore[index]
            importance_confidence=round(importance[1], 2),  # type: ignore[index]
        )
    note = ""
    if importance is not None:
        note = f" · Jev unsure ({importance[1]:.0%}), not used"
    return PriorityItem(
        id=load.id, score=score, band=band, source="heuristic",
        reason=f"{when} · {load.power_kw:g} kW · {flex}{note} — {verdict}",
    )


def prioritize(loads: list[PriorityLoad], api_key: str | None = None) -> PriorityResponse:
    """Rank `loads`, most pressing first. Never raises for a Jev failure."""
    notes: list[str] = []
    importances: dict[str, tuple[float, float]] = {}

    if not jev_client.resolve_api_key(api_key):
        notes.append("Jev is not configured; ranked with the built-in heuristic.")
    else:
        def one(load: PriorityLoad) -> tuple[str, Optional[tuple[float, float]], Optional[str]]:
            try:
                return load.id, _ask_importance(load, api_key), None
            except JevError as exc:
                return load.id, None, str(exc)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(one, loads))
        errors = {err for _, _, err in results if err}
        for lid, val, _err in results:
            if val is not None:
                importances[lid] = val
        if errors:
            notes.append(f"Jev unavailable for {sum(1 for _, v, _ in results if v is None)} load(s): {sorted(errors)[0]}.")

    items = [_score_one(load, importances.get(load.id)) for load in loads]
    items.sort(key=lambda i: (-i.score, i.id))
    provider = "jev" if any(i.source == "jev" for i in items) else "heuristic"
    return PriorityResponse(provider=provider, items=items, notes=notes)
