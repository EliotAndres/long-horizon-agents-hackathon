"""Deterministic flight market. This is the WORLD, not the agent: the agent only ever sees it through
Tinybird (observations) and Nimble (web verification)."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import from_minutes, to_minutes
from . import scenarios


@dataclass
class SimFlight:
    flight_id: str
    carrier: str
    origin: str
    destination: str
    departure_min: int
    arrival_min: int
    price: float
    stops: int
    available: bool
    band: tuple[float, float]


@dataclass
class Market:
    run_id: str
    seed: int = 7
    travel_date: str = scenarios.TRAVEL_DATE
    flights: dict[str, SimFlight] = field(default_factory=dict)
    clock: datetime = field(default_factory=lambda: datetime.fromisoformat(scenarios.SIM_START))
    seq: int = 0
    changes: int = 0

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        if not self.flights:
            for fid, carrier, origin, dep, arr, price, stops, avail, band in scenarios.FLIGHTS:
                self.flights[fid] = SimFlight(
                    fid, carrier, origin, "LAX", to_minutes(dep), to_minutes(arr), price, stops, avail, band
                )

    # --- observations -------------------------------------------------------------------------
    def observe(self, f: SimFlight) -> dict:
        self.seq += 1
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "observed_at": self.clock.strftime("%Y-%m-%d %H:%M:%S.000"),
            "flight_id": f.flight_id,
            "carrier": f.carrier,
            "origin": f.origin,
            "destination": f.destination,
            "travel_date": self.travel_date,
            "departure": from_minutes(f.departure_min),
            "arrival": from_minutes(f.arrival_min),
            "departure_min": f.departure_min,
            "arrival_min": f.arrival_min,
            "price": round(f.price, 2),
            "currency": "USD",
            "stops": f.stops,
            "available": int(f.available),
            "source": "market-sim",
            "source_url": f"sandbox://fares/{self.travel_date}/{f.flight_id}",
        }

    def snapshot(self) -> list[dict]:
        return [self.observe(f) for f in self.flights.values()]

    def _jitter(self, f: SimFlight) -> None:
        lo, hi = f.band
        step = self.rng.choice([-4, -3, -2, -1, 1, 2, 3, 4])
        new = min(hi, max(lo, f.price + step))
        if new != f.price:
            f.price = new
            self.changes += 1

    def tick(self) -> list[dict]:
        """Background market noise: a few prices move, but never across a flight's band."""
        self.clock += timedelta(minutes=1)
        picks = self.rng.sample(list(self.flights.values()), self.rng.randint(2, 4))
        for f in picks:
            self._jitter(f)
        return [self.observe(f) for f in picks]

    def timelapse(self, hours: int = scenarios.TIMELAPSE_HOURS, batches: int = 12) -> list[list[dict]]:
        """Fast-forward: every flight re-priced every 5 simulated minutes, grouped into batches."""
        steps_per_batch = hours * 12 // batches
        out = []
        for _ in range(batches):
            rows: list[dict] = []
            for _ in range(steps_per_batch):
                self.clock += timedelta(minutes=5)
                for f in self.flights.values():
                    self._jitter(f)
                    rows.append(self.observe(f))
            out.append(rows)
        return out

    # --- external world events (demo controls) ---------------------------------------------------
    def fare_sale(self) -> list[dict]:
        self.clock += timedelta(minutes=5)
        rows = []
        for fid, change in scenarios.FARE_SALE.items():
            f = self.flights[fid]
            f.price, f.band = change["price"], change["band"]
            self.changes += 1
            rows.append(self.observe(f))
        return rows

    def schedule_change(self, flight_id: str, slip_min: int = scenarios.SCHEDULE_SLIP_MIN) -> list[dict]:
        self.clock += timedelta(minutes=5)
        f = self.flights[flight_id]
        f.departure_min += slip_min
        f.arrival_min += slip_min
        self.changes += 1
        return [self.observe(f)]

    def view(self) -> list[dict]:
        """Current world state for the dashboard's market table."""
        return [
            {
                "flight_id": f.flight_id,
                "carrier": f.carrier,
                "origin": f.origin,
                "destination": f.destination,
                "departure": from_minutes(f.departure_min),
                "arrival": from_minutes(f.arrival_min),
                "price": round(f.price, 2),
                "stops": f.stops,
                "available": f.available,
            }
            for f in self.flights.values()
        ]
