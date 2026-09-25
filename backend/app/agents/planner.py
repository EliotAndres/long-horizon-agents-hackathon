"""Candidate selection and watch-policy generation (Liquid), behind a deterministic guardrail.

Liquid decides; the guardrail only enforces the user's hard constraints and booking policy. If Liquid
is unreachable or its choice violates a hard constraint, a transparent deterministic ranking is used
and the dashboard says so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..integrations.liquid import Liquid, LiquidError
from ..models import ActiveBooking, CandidateFlight, TravelIntent, justification, to_minutes, violations
from ..watcher.conditions import POST_BOOKING, TEMPLATES
from . import context as ctxmod

SYSTEM = """You are HorizonBook, an autonomous agent that owns a traveler's flight intent until the trip happens.
A monitoring trigger woke you. Decide what to do with the current candidates.
Every candidate already satisfies the traveler's hard constraints (price, arrival, departure).
Compare candidates using the traveler's preferences, strictly in this order:
 1. if prefer_nonstop is true, any candidate with nonstop=true beats every candidate with nonstop=false
 2. lower price_usd
 3. preferred_origin=true
 4. larger minutes_before_deadline
First copy each candidate's facts exactly as listed (nonstop, price_usd, preferred_origin).
Then "ranking": every candidate once, best first, applying the order above. flight_id = ranking[0].
action BOOK = book flight_id now (an active booking will be replaced). KEEP = keep the active booking.
WAIT = book nothing and keep watching. If auto_book is true, candidates exist and there is no ACTIVE booking, BOOK.
Finally choose the watch conditions that must wake you after this decision, from these templates:
{templates}
min_savings is how many USD a better candidate must save to be worth switching (typically 10-30).
"reasoning" is one sentence for the traveler explaining the choice.

Example (prefer_nonstop=true, preferred origins: SFO):
- AB1 | SFO -> LAX | $150 | 1 stop | preferred origin: yes
- CD2 | OAK -> LAX | $160 | nonstop | preferred origin: no
- EF3 | SFO -> LAX | $170 | nonstop | preferred origin: yes
ranking = ["CD2", "EF3", "AB1"]: CD2 and EF3 are nonstop so both beat AB1; CD2 is cheaper than EF3.
Return only the JSON object."""


@dataclass
class Decision:
    action: str
    candidate: CandidateFlight | None
    reasoning: str
    justified_by: list[str]
    watch: list[dict]
    decided_by: str
    meta: dict[str, Any] = field(default_factory=dict)
    guardrail: str = ""


def schema(candidates: list[CandidateFlight], has_booking: bool) -> dict[str, Any]:
    ids = [c.flight_id for c in candidates] + ["NONE"]
    actions = ["BOOK", "KEEP", "WAIT"] if has_booking else ["BOOK", "WAIT"]
    return {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "flight_id": {"type": "string", "enum": ids},
                        "nonstop": {"type": "boolean"},
                        "price_usd": {"type": "number"},
                        "preferred_origin": {"type": "boolean"},
                    },
                    "required": ["flight_id", "nonstop", "price_usd", "preferred_origin"],
                },
            },
            "ranking": {"type": "array", "items": {"type": "string", "enum": ids}, "maxItems": 6},
            "flight_id": {"type": "string", "enum": ids},
            "action": {"type": "string", "enum": actions},
            "reasoning": {"type": "string", "maxLength": 240},
            "watch": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": sorted(POST_BOOKING)},
                        "min_savings": {"type": "number"},
                    },
                    "required": ["type", "min_savings"],
                },
            },
        },
        "required": ["facts", "ranking", "flight_id", "action", "reasoning", "watch"],
    }


def rank(intent: TravelIntent, candidates: list[CandidateFlight]) -> list[CandidateFlight]:
    pref = intent.preferences
    deadline = to_minutes(intent.hard_constraints.arrival_before)

    def key(c: CandidateFlight) -> tuple:
        return (
            pref.prefer_nonstop and c.stops > 0,
            c.price + (0 if c.origin in pref.preferred_origins else 8),
            -(deadline - to_minutes(c.arrival)),
        )

    return sorted(candidates, key=key)


def _fallback(intent: TravelIntent, candidates: list[CandidateFlight], why: str) -> Decision:
    ok = [c for c in rank(intent, candidates) if not violations(intent, c)]
    best = ok[0] if ok else None
    return Decision(
        action="BOOK" if best and intent.booking_policy.auto_book else "WAIT",
        candidate=best,
        reasoning=f"Deterministic ranking (nonstop, price, preferred origin) because {why}.",
        justified_by=justification(intent, best) if best else [],
        watch=[
            {"type": "arrival_after"},
            {"type": "availability_changed"},
            {"type": "better_candidate", "parameters": {"min_savings": 15}},
        ],
        decided_by="deterministic-fallback",
        guardrail=why,
    )


async def choose(
    liquid: Liquid | None,
    intent: TravelIntent,
    trigger: dict[str, Any],
    candidates: list[CandidateFlight],
    booking: ActiveBooking | None,
    web: dict[str, Any] | None,
) -> Decision:
    valid_booking = booking if booking and booking.status == "ACTIVE" else None
    ctx = ctxmod.build(intent, trigger, candidates, booking, web)
    if liquid is None:
        return _fallback(intent, candidates, "Liquid is not configured")
    templates = "\n".join(f" - {k}: {v}" for k, v in TEMPLATES.items() if k in POST_BOOKING)
    try:
        out = await liquid.chat_json(
            "choose_flight",
            SYSTEM.format(templates=templates),
            ctxmod.render(ctx),
            schema(candidates, valid_booking is not None),
            max_tokens=1500,
        )
    except LiquidError as e:
        return _fallback(intent, candidates, f"Liquid call failed ({e})")
    meta = out.pop("_meta")
    meta["context_bytes"] = ctxmod.size_bytes(ctx)
    meta["context"] = ctx
    meta["output"] = json.dumps(out)
    by_id = {c.flight_id: c for c in candidates}
    action, fid = out.get("action"), out.get("flight_id")
    chosen = by_id.get(fid or "")

    # Guardrail: hard constraints and booking policy are the user's, not the model's.
    problem = ""
    if action == "BOOK" and not chosen:
        problem = f"BOOK without a valid candidate ({fid})"
    elif action == "BOOK" and chosen and violations(intent, chosen):
        problem = f"{fid} violates {violations(intent, chosen)}"
    elif action == "BOOK" and valid_booking and chosen and chosen.flight_id == valid_booking.candidate.flight_id:
        action = "KEEP"
    elif action in ("WAIT", "KEEP") and candidates and not valid_booking and intent.booking_policy.auto_book:
        problem = f"{action} although auto_book is on and {len(candidates)} candidate(s) satisfy every hard constraint"
    if problem:
        d = _fallback(intent, candidates, f"guardrail rejected Liquid's output: {problem}")
        d.meta = meta
        return d

    return Decision(
        action=action or "WAIT",
        candidate=chosen if action == "BOOK" else None,
        reasoning=str(out.get("reasoning", ""))[:400],
        justified_by=justification(intent, chosen) if chosen and action == "BOOK" else [],
        watch=[
            {"type": w.get("type"), "parameters": {"min_savings": w.get("min_savings")}} for w in out.get("watch", [])
        ],
        decided_by=liquid.label,
        meta=meta,
    )
