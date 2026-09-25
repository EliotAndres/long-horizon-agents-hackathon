"""Watch-condition templates. Each type maps to one deployed Tinybird endpoint plus a firing rule.

Liquid proposes conditions by choosing among these templates and filling parameters; this module
validates them and fills anything unsafe or missing from the durable intent.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from ..models import FLIGHT, ActiveBooking, TravelIntent, WatchCondition, to_minutes

TEMPLATES = {
    "price_below": "a flight satisfying every hard constraint is available at or under the budget",
    "better_candidate": "another qualifying flight is cheaper than the booked one by at least min_savings",
    "arrival_after": "the booked flight's arrival moves later than the deadline (or departure earlier than allowed)",
    "availability_changed": "the booked flight is no longer available",
}
PRE_BOOKING = {"price_below"}
POST_BOOKING = {"better_candidate", "arrival_after", "availability_changed"}


@dataclass
class Firing:
    condition: WatchCondition
    rows: list[dict]
    fingerprint: str
    summary: str


def _constraint_params(intent: TravelIntent) -> dict[str, Any]:
    hc = intent.hard_constraints
    return {
        "travel_date": intent.travel_date,
        "destination": intent.destination,
        "origins": hc.allowed_origins,
        "max_price": hc.max_price,
        "arrival_before_min": to_minutes(hc.arrival_before),
        "departure_after_min": to_minutes(hc.departure_after),
    }


def qualifying_query(intent: TravelIntent, run_id: str) -> tuple[str, dict[str, Any]]:
    return "qualifying_candidates", {"run_id": run_id, **_constraint_params(intent)}


def query_for(c: WatchCondition, intent: TravelIntent, run_id: str) -> tuple[str, dict[str, Any]]:
    """Return (tinybird pipe, params). Hard-constraint params always come from the durable intent."""
    base = {"run_id": run_id, **_constraint_params(intent)}
    if c.type == "price_below":
        return qualifying_query(intent, run_id)
    if c.type == "better_candidate":
        booked_price = float(c.parameters["booked_price"])
        savings = float(c.parameters.get("min_savings", 15))
        return "qualifying_candidates", {
            **base,
            "max_price": min(intent.hard_constraints.max_price, booked_price - savings),
            "exclude_flight": c.parameters["flight_id"],
        }
    if c.type in ("arrival_after", "availability_changed"):
        return "plan_health", {
            "run_id": run_id,
            "flight_id": c.parameters["flight_id"],
            "arrival_before_min": base["arrival_before_min"],
            "departure_after_min": base["departure_after_min"],
        }
    raise ValueError(c.type)


def evaluate(c: WatchCondition, rows: list[dict]) -> Firing | None:
    """Apply the firing rule to the endpoint's rows. Edge-triggered via a fingerprint of the evidence."""
    if c.type in ("price_below", "better_candidate"):
        fired = rows
        summary = ", ".join(f"{r['flight_id']} ${r['price']:.0f}" for r in rows)
    elif c.type == "arrival_after":
        fired = [r for r in rows if r["arrival_violated"] or r["departure_violated"]]
        summary = ", ".join(f"{r['flight_id']} now {r['departure']}->{r['arrival']}" for r in fired)
    elif c.type == "availability_changed":
        fired = [r for r in rows if r["unavailable"]]
        summary = ", ".join(f"{r['flight_id']} no longer available" for r in fired)
    else:
        raise ValueError(c.type)
    if not fired:
        return None
    # Opportunities are keyed by WHICH flights qualify (price noise inside the set is not news);
    # plan conditions are keyed by the booked flight's schedule/availability.
    if c.type in ("price_below", "better_candidate"):
        key: list = sorted(r["flight_id"] for r in fired)
    else:
        key = [(r["flight_id"], r["arrival_min"], r["departure_min"], r["available"]) for r in fired]
    fp = hashlib.sha1(json.dumps(key).encode(), usedforsecurity=False).hexdigest()[:12]
    if fp == c.last_fingerprint:
        return None
    return Firing(c, fired, fp, summary)


def describe(c: WatchCondition, intent: TravelIntent) -> str:
    hc = intent.hard_constraints
    p = c.parameters
    if c.type == "price_below":
        return f"price <= ${hc.max_price:.0f} & arrive <= {hc.arrival_before} & depart >= {hc.departure_after}"
    if c.type == "better_candidate":
        savings = float(p.get("min_savings", 15))
        return f"alternative <= ${float(p['booked_price']) - savings:.0f} (saves >= ${savings:.0f})"
    if c.type == "arrival_after":
        return f"{p['flight_id']} arrival > {hc.arrival_before} or departure < {hc.departure_after}"
    if c.type == "availability_changed":
        return f"{p['flight_id']} becomes unavailable"
    return c.type


def _new(intent: TravelIntent, type_: str, params: dict, by: str) -> WatchCondition:
    # model_validate checks type_ against the ConditionType literal at runtime.
    return WatchCondition.model_validate(
        {
            "id": f"wc-{uuid.uuid4().hex[:8]}",
            "intent_id": intent.id,
            "type": type_,
            "parameters": params,
            "created_by": by,
        }
    )


def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def build_policy(
    intent: TravelIntent, booking: ActiveBooking | None, proposed: list[dict] | None, by: str
) -> list[WatchCondition]:
    """Turn a (possibly model-proposed) watch policy into safe conditions.

    - Only known templates, only the phase-appropriate ones.
    - Flight ids and prices are bound to the actual booking, not to whatever the model wrote.
    - Safety floor: an active plan is always guarded for arrival and availability.
    """
    allowed = POST_BOOKING if booking else PRE_BOOKING
    chosen: dict[str, tuple[dict, str]] = {}
    for item in proposed or []:
        t = str(item.get("type", ""))
        if t in allowed and t not in chosen:
            chosen[t] = (dict(item.get("parameters") or {}), by)
    for t in ("arrival_after", "availability_changed") if booking else ("price_below",):
        chosen.setdefault(t, ({}, "policy-floor"))
    conds = []
    for t, (raw, src) in chosen.items():
        params: dict[str, Any] = {}
        if booking:
            assert FLIGHT.match(booking.candidate.flight_id)
            params["flight_id"] = booking.candidate.flight_id
        if t == "better_candidate" and booking:
            params["booked_price"] = booking.candidate.price
            params["min_savings"] = _clamp(raw.get("min_savings"), 5, 100, default=15)
        conds.append(_new(intent, t, params, src))
    return conds
