"""What Liquid sees on wake: durable state + the trigger + only the relevant current candidates.

No chat transcript, no market history. The context is rebuilt from persistent state every time,
which is why the agent can sleep for days (or restart) without losing the plot.
"""

from __future__ import annotations

import json
from typing import Any

from ..models import ActiveBooking, CandidateFlight, TravelIntent, to_minutes


def candidate_from_row(row: dict) -> CandidateFlight:
    return CandidateFlight(
        flight_id=row["flight_id"],
        carrier=row.get("carrier", ""),
        origin=row["origin"],
        destination=row["destination"],
        departure=row["departure"],
        arrival=row["arrival"],
        price=float(row["price"]),
        stops=int(row.get("stops", 0)),
        available=bool(row.get("available", 1)),
        source="tinybird:qualifying_candidates",
        source_url=row.get("source_url", ""),
        observed_at=str(row.get("observed_at", "")),
    )


def build(
    intent: TravelIntent,
    trigger: dict[str, Any],
    candidates: list[CandidateFlight],
    booking: ActiveBooking | None,
    web: dict[str, Any] | None,
) -> dict[str, Any]:
    hc, pref = intent.hard_constraints, intent.preferences
    ctx: dict[str, Any] = {
        "intent": {
            "route": f"{'/'.join(hc.allowed_origins)} -> {intent.destination}",
            "date": intent.travel_date,
            "hard_constraints": {
                "arrive_by": hc.arrival_before,
                "depart_not_before": hc.departure_after,
                "max_price_usd": hc.max_price,
            },
            "preferences": {"prefer_nonstop": pref.prefer_nonstop, "preferred_origins": pref.preferred_origins},
            "auto_book": intent.booking_policy.auto_book,
        },
        "trigger": trigger,
        "active_booking": None,
        "candidates": [
            {
                "flight_id": c.flight_id,
                "carrier": c.carrier,
                "from": c.origin,
                "depart": c.departure,
                "arrive": c.arrival,
                "price_usd": c.price,
                "stops": c.stops,
                "nonstop": c.stops == 0,
                "preferred_origin": c.origin in pref.preferred_origins,
                "minutes_before_deadline": to_minutes(hc.arrival_before) - to_minutes(c.arrival),
                "web_sources": (web or {}).get("per_flight", {}).get(c.flight_id, 0),
            }
            for c in candidates
        ],
    }
    if booking:
        b = booking.candidate
        ctx["active_booking"] = {
            "booking_id": booking.booking_id,
            "flight_id": b.flight_id,
            "status": booking.status,
            "depart": b.departure,
            "arrive": b.arrival,
            "paid_usd": b.price,
        }
    return ctx


def size_bytes(ctx: dict[str, Any]) -> int:
    return len(json.dumps(ctx, separators=(",", ":")))


def render(ctx: dict[str, Any]) -> str:
    """Plain-text view of the context for a small model: one line per fact, one row per candidate."""
    i, hc, pref, t = ctx["intent"], ctx["intent"]["hard_constraints"], ctx["intent"]["preferences"], ctx["trigger"]
    lines = [
        f"TRIGGER: {t['type']} -> {t['summary']}"
        + (" The active plan is INVALID and must be replaced." if t.get("plan_invalid") else ""),
        f"TRAVELER: {i['route']} on {i['date']}. Arrive by {hc['arrive_by']}, depart not before "
        f"{hc['depart_not_before']}, budget ${hc['max_price_usd']:.0f}.",
        f"PREFERENCES: prefer_nonstop={str(pref['prefer_nonstop']).lower()}, preferred origins: "
        f"{', '.join(pref['preferred_origins'])}. auto_book={str(i['auto_book']).lower()}.",
    ]
    b = ctx.get("active_booking")
    lines.append(
        f"ACTIVE BOOKING: {b['booking_id']} {b['flight_id']} {b['depart']}->{b['arrive']} "
        f"${b['paid_usd']:.0f} status={b['status']}"
        if b
        else "ACTIVE BOOKING: none"
    )
    lines.append("CANDIDATES (each one satisfies every hard constraint):")
    for c in ctx["candidates"]:
        lines.append(
            f"- {c['flight_id']} | {c['carrier']} | {c['from']} {c['depart']} -> {c['arrive']} | "
            f"${c['price_usd']:.0f} | "
            f"{'nonstop' if c['nonstop'] else str(c['stops']) + ' stop'} | preferred origin: "
            f"{'yes' if c['preferred_origin'] else 'no'} | {c['minutes_before_deadline']} min before deadline"
            + (f" | seen on {c['web_sources']} live web source(s)" if c["web_sources"] else "")
        )
    if not ctx["candidates"]:
        lines.append("- none")
    return "\n".join(lines)
