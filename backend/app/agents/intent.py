"""Intent compilation: natural-language request -> durable TravelIntent (Liquid)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta

from ..integrations.liquid import Liquid
from ..models import BookingPolicy, HardConstraints, Preferences, TravelIntent
from ..store import now_iso

SCHEMA = {
    "type": "object",
    "properties": {
        "origin": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "allowed_origins": {"type": "array", "items": {"type": "string", "pattern": "^[A-Z]{3}$"}, "maxItems": 4},
        "destination": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "travel_date": {"type": "string", "pattern": "^2026-[01][0-9]-[0-3][0-9]$"},
        "arrival_before": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
        "departure_after": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
        "max_price": {"type": "number"},
        "prefer_nonstop": {"type": "boolean"},
        "preferred_origins": {"type": "array", "items": {"type": "string", "pattern": "^[A-Z]{3}$"}, "maxItems": 3},
        "auto_book": {"type": "boolean"},
    },
    "required": [
        "origin",
        "allowed_origins",
        "destination",
        "travel_date",
        "arrival_before",
        "departure_after",
        "max_price",
        "prefer_nonstop",
        "preferred_origins",
        "auto_book",
    ],
}

SYSTEM = """You compile a traveler's flight request into a structured, durable travel intent.
Rules:
- Use IATA airport codes. San Francisco area airports: SFO, OAK. Los Angeles: LAX.
- allowed_origins: every departure airport the traveler accepts. preferred_origins: the ones they favour.
- Times are 24h HH:MM. "arrive before 9 AM" -> arrival_before "09:00". "not before 5 AM" -> departure_after "05:00".
- If no departure limit is given use "00:00"; if no arrival limit use "23:59".
- travel_date is YYYY-MM-DD, resolved against today's date.
- max_price is the flight budget in USD as a number.
- auto_book is true only if the traveler allows booking without asking.
Return only the JSON object."""


async def compile_intent(liquid: Liquid, text: str, today: date) -> tuple[TravelIntent, dict]:
    """Returns (intent, liquid_meta). Raises LiquidError/ValidationError if the model output is unusable."""
    user = f"Today is {today.isoformat()} ({today.strftime('%A')}).\nRequest: {text}"
    out = await liquid.chat_json("compile_intent", SYSTEM, user, SCHEMA, max_tokens=1200)
    meta = out.pop("_meta")
    intent = TravelIntent(
        id=f"intent-{uuid.uuid4().hex[:8]}",
        request_text=text,
        origin=out["origin"],
        destination=out["destination"],
        travel_date=out["travel_date"],
        hard_constraints=HardConstraints(
            arrival_before=out["arrival_before"],
            departure_after=out["departure_after"],
            max_price=out["max_price"],
            allowed_origins=sorted(set(out["allowed_origins"]) | {out["origin"]}),
        ),
        preferences=Preferences(
            prefer_nonstop=out["prefer_nonstop"], preferred_origins=out["preferred_origins"] or [out["origin"]]
        ),
        booking_policy=BookingPolicy(auto_book=out["auto_book"]),
        compiled_by=liquid.label,
        created_at=now_iso(),
    )
    meta["output"] = json.dumps(out)
    return intent, meta


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def fallback_intent(text: str, today: date) -> TravelIntent:
    """Deterministic parser used only when Liquid is unreachable, labelled as such in the UI."""
    t = text.lower()

    def hour(pattern: str, default: str) -> str:
        m = re.search(pattern + r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
        if not m:
            return default
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        return f"{h:02d}:{m.group(2) or '00'}"

    price = re.search(r"\$\s?(\d{2,5})", t)
    when = today + timedelta(days=1)
    for i, name in enumerate(_WEEKDAYS):
        if name in t:
            when = today + timedelta(days=(i - today.weekday()) % 7 or 7)
    origins = [c for c, k in (("SFO", "sfo"), ("OAK", "oakland")) if k in t or "san francisco" in t]
    return TravelIntent(
        id=f"intent-{uuid.uuid4().hex[:8]}",
        request_text=text,
        origin="SFO",
        destination="LAX",
        travel_date=when.isoformat(),
        hard_constraints=HardConstraints(
            arrival_before=hour(r"arrive before", "23:59"),
            departure_after=hour(r"depart before", "00:00"),
            max_price=float(price.group(1)) if price else 250,
            allowed_origins=origins or ["SFO"],
        ),
        preferences=Preferences(prefer_nonstop="nonstop" in t, preferred_origins=["SFO"]),
        booking_policy=BookingPolicy(auto_book="book automatically" in t),
        compiled_by="fallback-parser",
        created_at=now_iso(),
    )
