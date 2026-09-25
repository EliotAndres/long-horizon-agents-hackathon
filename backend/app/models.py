"""Durable domain model. These objects ARE the agent's memory: no chat transcript is kept."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

IATA = re.compile(r"^[A-Z]{3}$")
FLIGHT = re.compile(r"^[A-Z0-9]{2,8}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def from_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class HardConstraints(BaseModel):
    arrival_before: str = "09:00"
    departure_after: str = "05:00"
    max_price: float = Field(180, gt=0, le=10_000)
    allowed_origins: list[str] = ["SFO", "OAK"]

    @field_validator("arrival_before", "departure_after")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError(f"expected HH:MM, got {v!r}")
        return v

    @field_validator("allowed_origins")
    @classmethod
    def _iata_list(cls, v: list[str]) -> list[str]:
        v = [x.upper() for x in v]
        if not v or not all(IATA.match(x) for x in v):
            raise ValueError(f"bad airport codes {v!r}")
        return v


class Preferences(BaseModel):
    prefer_nonstop: bool = True
    preferred_origins: list[str] = ["SFO"]


class BookingPolicy(BaseModel):
    auto_book: bool = True


IntentStatus = Literal["MONITORING", "BOOKED", "REPLANNING", "COMPLETE"]


class TravelIntent(BaseModel):
    id: str
    request_text: str
    origin: str
    destination: str
    travel_date: str
    hard_constraints: HardConstraints
    preferences: Preferences
    booking_policy: BookingPolicy
    status: IntentStatus = "MONITORING"
    compiled_by: str = ""
    created_at: str = ""

    @field_validator("origin", "destination")
    @classmethod
    def _iata(cls, v: str) -> str:
        v = v.upper()
        if not IATA.match(v):
            raise ValueError(f"bad airport code {v!r}")
        return v

    @field_validator("travel_date")
    @classmethod
    def _date(cls, v: str) -> str:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError(f"expected YYYY-MM-DD, got {v!r}")
        return v


class CandidateFlight(BaseModel):
    flight_id: str
    carrier: str = ""
    origin: str
    destination: str
    travel_date: str = ""
    departure: str
    arrival: str
    price: float
    currency: str = "USD"
    stops: int = 0
    available: bool = True
    source: str = ""
    source_url: str = ""
    observed_at: str = ""


BookingStatus = Literal["ACTIVE", "INVALID", "CANCELLED"]


class ActiveBooking(BaseModel):
    booking_id: str
    intent_id: str
    candidate: CandidateFlight
    created_at: str
    status: BookingStatus = "ACTIVE"
    justified_by: list[str] = []
    reasoning: str = ""
    decided_by: str = ""


ConditionType = Literal["price_below", "arrival_after", "availability_changed", "better_candidate"]


class WatchCondition(BaseModel):
    id: str
    intent_id: str
    type: ConditionType
    parameters: dict[str, Any] = {}
    armed: bool = True
    created_by: str = ""
    last_fingerprint: str = ""
    fired_count: int = 0


class Event(BaseModel):
    id: int = 0
    timestamp: str
    type: str
    source: str
    message: str
    payload: dict[str, Any] = {}


def violations(intent: TravelIntent, c: CandidateFlight) -> list[str]:
    """Deterministic hard-constraint check. Used by the UI and as the guardrail on Liquid's choices."""
    hc = intent.hard_constraints
    out: list[str] = []
    if not c.available:
        out.append("SOLD OUT")
    if c.origin not in hc.allowed_origins or c.destination != intent.destination:
        out.append("ROUTE")
    if c.price > hc.max_price:
        out.append("PRICE")
    if to_minutes(c.departure) < to_minutes(hc.departure_after):
        out.append("DEPARTS EARLY")
    if to_minutes(c.arrival) > to_minutes(hc.arrival_before):
        out.append("ARRIVES LATE")
    return out


def justification(intent: TravelIntent, c: CandidateFlight) -> list[str]:
    hc = intent.hard_constraints
    return [
        f"price ${c.price:.0f} <= ${hc.max_price:.0f}",
        f"arrival {c.arrival} <= {hc.arrival_before}",
        f"departure {c.departure} >= {hc.departure_after}",
        f"origin {c.origin} in {'/'.join(hc.allowed_origins)}",
    ]
