"""The two paths that matter, end to end through the real app wiring:

1. persistent intent -> price drop -> condition fires -> Liquid decides -> sandbox booking
2. booking -> schedule change -> invalidation -> Liquid replans -> cancel old + book replacement

Tinybird is replaced by LocalAnalytics (same endpoint semantics) and Liquid by a scripted fake, so
this runs offline. tests/test_integration_live.py exercises the real services.
"""

from __future__ import annotations

import httpx
import pytest

from app.booking.sandbox import SandboxClient
from app.config import Settings
from app.integrations.liquid import Liquid
from app.integrations.nimble import Nimble
from app.integrations.tinybird import LocalAnalytics
from app.main import App, build_app
from app.store import Store


class ScriptedLiquid(Liquid):
    """Answers like an LFM model would, and records every prompt it was given."""

    def __init__(self) -> None:
        super().__init__("http://liquid.invalid/v1", "LFM-test")
        self.prompts: list[tuple[str, str]] = []

    async def chat_json(self, task, system, user, schema, max_tokens=700):
        self.prompts.append((task, user))
        if task == "compile_intent":
            out = {
                "origin": "SFO",
                "allowed_origins": ["SFO", "OAK"],
                "destination": "LAX",
                "travel_date": "2026-09-28",
                "arrival_before": "09:00",
                "departure_after": "05:00",
                "max_price": 180,
                "prefer_nonstop": True,
                "preferred_origins": ["SFO"],
                "auto_book": True,
            }
        else:
            rows = [ln.split(" | ") for ln in user.splitlines() if ln.startswith("- ") and "nonstop" in ln]
            nonstop = sorted(rows, key=lambda r: float(r[3].lstrip("$")))
            pick = nonstop[0][0][2:] if nonstop else "NONE"
            out = {
                "action": "BOOK" if pick != "NONE" else "WAIT",
                "flight_id": pick,
                "reasoning": f"{pick} is the cheapest nonstop that meets every constraint",
                "facts": [],
                "ranking": [r[0][2:] for r in nonstop],
                "watch": [
                    {"type": "arrival_after", "min_savings": 0},
                    {"type": "better_candidate", "min_savings": 20},
                ],
            }
        meta = {"task": task, "model": self.model, "total_tokens": 100, "prompt_tokens": 80, "completion_tokens": 20}
        if self.on_usage:
            self.on_usage(meta)
        return {**out, "_meta": meta}


@pytest.fixture
async def hb(tmp_path):
    settings = Settings(db_path=":memory:", replay_dir=str(tmp_path), step_delay=0, watch_interval=0.01)
    liquid = ScriptedLiquid()
    hb = App(
        settings,
        Store(":memory:"),
        LocalAnalytics(),
        liquid,
        Nimble("http://nimble.invalid", "", str(tmp_path)),
        SandboxClient("http://sandbox"),
    )
    app = build_app(hb)
    app.state.hb, app.state.store, app.state.inventory_lookup = hb, hb.store, hb.inventory
    hb.runtime.sandbox = SandboxClient("http://sandbox", transport=httpx.ASGITransport(app=app))
    await hb.reset()
    yield hb
    if hb.world:
        hb.world.stop()


async def settle(hb: App) -> str | None:
    """One watcher pass; if Tinybird's condition fires, run the wake cycle. Returns the fired type."""
    firing = await hb.watcher.check_once()
    if firing:
        await hb.runtime.wake(firing)
        return firing.condition.type
    return None


async def test_price_drop_books_then_schedule_change_rebooks(hb: App):
    await hb.runtime.accept_intent(hb.state()["demo_request"], __import__("datetime").date(2026, 9, 25))
    intent = hb.store.intent()
    assert intent and intent.status == "MONITORING"
    assert intent.hard_constraints.max_price == 180
    assert hb.runtime.status == "SLEEPING"

    # Two simulated days of noise: Tinybird checks keep running, Liquid is never called.
    calls_before = len(hb.liquid.prompts)
    for batch in hb.world.market.timelapse():
        await hb.world.publish(batch)
        assert await settle(hb) is None
    m = hb.state()["metrics"]
    assert len(hb.liquid.prompts) == calls_before
    assert m["liquid_tokens_while_waiting"] == 0
    assert m["tinybird_checks"] >= 12 and m["market_observations"] > 4000

    # MAGIC MOMENT 1: the world changes once; everything after is automatic.
    await hb.world.fare_sale()
    assert await settle(hb) == "price_below"
    booking = hb.store.active_booking(intent.id)
    assert booking and booking.candidate.flight_id == "UA456" and booking.status == "ACTIVE"
    assert booking.booking_id == "HX-48321"
    assert hb.store.intent().status == "BOOKED"
    assert hb.runtime.status == "SLEEPING"
    armed = {c.type for c in hb.store.conditions(intent.id)}
    assert armed == {"arrival_after", "availability_changed", "better_candidate"}
    # Liquid saw only durable state + trigger + candidates.
    ctx = [e for e in hb.store.events() if e.type == "LIQUID"][-1].payload["context"]
    assert set(ctx) == {"intent", "trigger", "active_booking", "candidates"}
    assert {c["flight_id"] for c in ctx["candidates"]} == {"UA456", "WN1402"}

    # Noise after booking is not news.
    for _ in range(5):
        await hb.world.publish(hb.world.market.tick())
        assert await settle(hb) is None

    # MAGIC MOMENT 2: the airline retimes the booked flight.
    await hb.world.schedule_change("UA456")
    assert await settle(hb) == "arrival_after"
    bookings = {b.candidate.flight_id: b for b in hb.store.bookings(intent.id)}
    assert bookings["UA456"].status == "CANCELLED"
    assert bookings["WN1402"].status == "ACTIVE"
    assert hb.store.sandbox_get(booking_id=bookings["UA456"].booking_id)["status"] == "CANCELLED"
    assert hb.store.sandbox_get(booking_id=bookings["WN1402"].booking_id)["status"] == "CONFIRMED"
    ctx = [e for e in hb.store.events() if e.type == "LIQUID"][-1].payload["context"]
    assert ctx["trigger"]["plan_invalid"] is True
    assert "UA456" not in {c["flight_id"] for c in ctx["candidates"]}
    assert ctx["active_booking"]["status"] == "INVALID"
    assert {c.parameters["flight_id"] for c in hb.store.conditions(intent.id)} == {"WN1402"}
    assert hb.runtime.status == "SLEEPING"
    assert hb.state()["metrics"]["liquid_tokens_while_waiting"] == 0
    assert await settle(hb) is None


async def test_guardrail_rejects_constraint_violating_choice(hb: App):
    from datetime import date

    await hb.runtime.accept_intent("x", date(2026, 9, 25))
    await hb.world.fare_sale()

    async def bad(task, system, user, schema, max_tokens=700):
        return {
            "action": "WAIT",
            "flight_id": "NONE",
            "reasoning": "meh",
            "ranking": [],
            "watch": [],
            "_meta": {"total_tokens": 1},
        }

    hb.liquid.chat_json = bad  # auto_book is on and candidates qualify: WAIT is not allowed
    assert await settle(hb) == "price_below"
    b = hb.store.active_booking(hb.store.intent().id)
    assert b and b.decided_by == "deterministic-fallback" and b.candidate.flight_id == "UA456"
    assert any(e.type == "GUARDRAIL" for e in hb.store.events())


async def test_no_candidates_after_invalidation_keeps_watching(hb: App):
    from datetime import date

    await hb.runtime.accept_intent("x", date(2026, 9, 25))
    await hb.world.fare_sale()
    assert await settle(hb) == "price_below"  # UA456
    for fid in ("UA456", "WN1402", "UA1190"):
        await hb.world.schedule_change(fid)
    fired = await settle(hb)
    assert fired == "arrival_after"
    intent = hb.store.intent()
    assert intent.status == "MONITORING"
    assert {c.type for c in hb.store.conditions(intent.id)} == {"price_below"}
    assert hb.runtime.status == "SLEEPING"


async def test_travel_date_mismatch_never_fires(hb: App):
    from datetime import date

    await hb.runtime.accept_intent("x", date(2026, 9, 25))
    intent = hb.store.intent()
    intent.travel_date = "2026-09-29"  # market is for 2026-09-28
    hb.store.put_intent(intent)
    await hb.world.fare_sale()
    assert await settle(hb) is None


async def test_guardrail_rejects_booking_that_violates_hard_constraints(hb: App):
    from datetime import date

    from app.integrations.tinybird import LocalAnalytics

    await hb.runtime.accept_intent("x", date(2026, 9, 25))
    await hb.world.fare_sale()
    real_endpoint = LocalAnalytics.endpoint

    async def leaky(self, pipe, params):  # a buggy analytics layer also returns an over-budget flight
        rows = await real_endpoint(self, pipe, params)
        if pipe == "qualifying_candidates" and rows:
            aa = {**rows[0], "flight_id": "AA123", "price": 195.0, "origin": "OAK"}
            rows = rows + [aa]
        return rows

    hb.analytics.endpoint = leaky.__get__(hb.analytics)

    async def picks_violator(task, system, user, schema, max_tokens=700):
        return {
            "action": "BOOK",
            "flight_id": "AA123",
            "reasoning": "",
            "ranking": [],
            "watch": [],
            "facts": [],
            "_meta": {"total_tokens": 1},
        }

    hb.liquid.chat_json = picks_violator
    assert await settle(hb) == "price_below"
    b = hb.store.active_booking(hb.store.intent().id)
    assert b and b.candidate.flight_id == "UA456" and b.decided_by == "deterministic-fallback"


async def test_liquid_errors_fall_back_but_still_act(hb: App):
    from datetime import date

    from app.integrations.liquid import LiquidError

    async def down(*a, **k):
        raise LiquidError("connection refused")

    hb.liquid.chat_json = down
    intent = await hb.runtime.accept_intent(hb.state()["demo_request"], date(2026, 9, 25))
    assert intent.compiled_by == "fallback-parser" and intent.hard_constraints.max_price == 180
    await hb.world.fare_sale()
    assert await settle(hb) == "price_below"
    assert hb.store.active_booking(intent.id).decided_by == "deterministic-fallback"


async def test_failed_wake_retries_on_same_evidence(hb: App):
    from datetime import date

    await hb.runtime.accept_intent("x", date(2026, 9, 25))
    await hb.world.fare_sale()
    real_book = hb.runtime.sandbox.book

    async def flaky(*a, **k):
        raise RuntimeError("provider timeout")

    hb.runtime.sandbox.book = flaky
    assert await settle(hb) == "price_below"
    assert hb.store.active_booking(hb.store.intent().id) is None
    assert await settle(hb) is None  # backing off
    hb.store.set("retry_not_before", 0)
    hb.runtime.sandbox.book = real_book
    assert await settle(hb) == "price_below"  # same evidence fires again
    assert hb.store.active_booking(hb.store.intent().id).candidate.flight_id == "UA456"


async def test_wait_decision_does_not_loop(hb: App):
    from datetime import date

    await hb.runtime.accept_intent("x", date(2026, 9, 25))
    intent = hb.store.intent()
    intent.booking_policy.auto_book = False
    hb.store.put_intent(intent)

    async def waits(task, system, user, schema, max_tokens=700):
        return {
            "action": "WAIT",
            "flight_id": "NONE",
            "reasoning": "ask first",
            "ranking": [],
            "watch": [],
            "facts": [],
            "_meta": {"total_tokens": 1},
        }

    hb.liquid.chat_json = waits
    await hb.world.fare_sale()
    assert await settle(hb) == "price_below"
    for _ in range(3):
        await hb.world.publish(hb.world.market.tick())
        assert await settle(hb) is None
    assert hb.store.active_booking(intent.id) is None


async def test_failed_replacement_keeps_old_booking(hb: App):
    from datetime import date

    await hb.runtime.accept_intent("x", date(2026, 9, 25))
    await hb.world.fare_sale()
    assert await settle(hb) == "price_below"
    old = hb.store.active_booking(hb.store.intent().id)

    async def sold_out(*a, **k):
        raise RuntimeError("409 sold out")

    hb.runtime.sandbox.book = sold_out
    await hb.world.schedule_change("UA456")
    assert await settle(hb) == "arrival_after"
    assert hb.store.sandbox_get(booking_id=old.booking_id)["status"] == "CONFIRMED"  # never released
    assert hb.store.active_booking(hb.store.intent().id).booking_id == old.booking_id
