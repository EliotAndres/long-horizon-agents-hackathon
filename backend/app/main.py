"""HorizonBook API.

Human-facing endpoints are deliberately limited to:
  * POST /api/intent          -- the user states the trip once
  * POST /api/world/*         -- EXTERNAL WORLD events for the demo (not agent controls)
  * POST /api/demo/reset      -- return everything to the initial condition
There is no "run agent", "replan" or "book" endpoint: the agent acts only when Tinybird wakes it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .agents.runtime import AgentRuntime
from .booking import sandbox
from .booking.sandbox import SandboxClient
from .config import Settings
from .integrations.liquid import Liquid
from .integrations.nimble import Nimble
from .integrations.tinybird import LocalAnalytics, Tinybird
from .models import CandidateFlight, violations
from .sim import scenarios
from .sim.market import Market
from .sim.world import World
from .store import Store
from .watcher import conditions as cond
from .watcher.worker import Watcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("horizonbook")

TODAY = date(2026, 9, 25)


class IntentIn(BaseModel):
    text: str = scenarios.DEMO_REQUEST


class App:
    """Process-wide wiring. Tests build one with fakes; the server builds one from Settings."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        analytics,
        liquid: Liquid | None,
        nimble: Nimble,
        sandbox_client: SandboxClient,
    ):
        self.settings, self.store, self.analytics = settings, store, analytics
        self.liquid, self.nimble = liquid, nimble
        self.runtime = AgentRuntime(store, analytics, liquid, nimble, sandbox_client, settings.step_delay)
        if liquid:
            liquid.on_usage = self.runtime.on_liquid_usage
        self.watcher = Watcher(store, analytics, self.runtime, settings.watch_interval)
        self.world: World | None = None
        self.watch_task: asyncio.Task | None = None
        self.bg: set[asyncio.Task] = set()
        self.liquid_ok = False

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.bg.add(t)
        t.add_done_callback(self.bg.discard)

    async def reset(self) -> None:
        if self.world:
            self.world.stop()
        for t in list(self.bg):
            t.cancel()
        async with self.runtime.lock:
            self.store.reset()
            run_id = uuid.uuid4().hex[:12]
            self.store.set("run_id", run_id)
            self.store.set("agent_status", "IDLE")
            world = World(
                self.store, self.analytics, Market(run_id=run_id), self.settings.tick_interval, self.watcher.nudge
            )
            await world.seed()
            self.world = world
            self.store.log("RESET", "world", f"Demo reset. New market run {run_id}.")

    def inventory(self, flight_id: str) -> dict | None:
        f = self.world.market.flights.get(flight_id) if self.world else None
        return {"available": f.available, "price": round(f.price, 2)} if f else None

    async def submit_intent(self, text: str) -> None:
        try:
            await self.runtime.accept_intent(text, TODAY)
        except Exception as e:  # surface background failures instead of leaving the UI stuck
            log.exception("intent handover failed")
            self.store.log("ERROR", "agent", f"Intent handover failed ({type(e).__name__}: {e}). Reset and retry.")
            if not self.store.intent():
                self.runtime.set_status("IDLE")
            return
        if self.world:
            self.world.start(timelapse=True)

    def state(self) -> dict:
        s = self.store
        intent = s.intent()
        m = s.metrics()
        booking = s.active_booking(intent.id) if intent else None
        market = self.world.market.view() if self.world else []
        for row in market:
            if intent:
                v = violations(intent, CandidateFlight(**row))
                row["violations"] = v
                row["qualifies"] = not v
            row["booked"] = bool(booking and booking.candidate.flight_id == row["flight_id"])
        conds = s.conditions(intent.id) if intent else []
        return {
            "agent_status": self.runtime.status,
            "run_id": s.get("run_id"),
            "intent": intent.model_dump() if intent else None,
            "booking": booking.model_dump() if booking else None,
            "bookings": [b.model_dump() for b in s.bookings(intent.id)] if intent else [],
            "conditions": [{**c.model_dump(), "rule": cond.describe(c, intent)} for c in conds] if intent else [],
            "market": market,
            "events": [e.model_dump() for e in s.events(160)],
            "metrics": {
                "market_observations": int(m.get("market_observations", 0)),
                "market_changes": int(s.get("market_changes", 0)),
                "tinybird_checks": int(m.get("tinybird_checks", 0)),
                "tinybird_fires": int(m.get("tinybird_fires", 0)),
                "nimble_calls": int(m.get("nimble_calls", 0)),
                "nimble_live": int(m.get("nimble_live", 0)),
                "liquid_calls": int(m.get("liquid_calls", 0)),
                "liquid_tokens": int(m.get("liquid_tokens", 0)),
                "liquid_tokens_while_waiting": int(m.get("liquid_tokens_while_waiting", 0)),
                "wakes": int(m.get("wakes", 0)),
                "bookings": int(m.get("bookings", 0)),
                "cancellations": int(m.get("cancellations", 0)),
            },
            "sleep_started_at": s.get("sleep_started_at"),
            "sim_clock": s.get("sim_clock"),
            "world_phase": self.world.phase if self.world else "idle",
            "integrations": {
                "tinybird": {
                    "mode": "live" if getattr(self.analytics, "live", False) else "LOCAL FALLBACK",
                    "host": getattr(self.analytics, "host", "in-process"),
                },
                "liquid": {
                    "mode": "live" if self.liquid_ok else "unreachable",
                    "model": self.liquid.model if self.liquid else None,
                },
                "nimble": {
                    "mode": s.get("nimble_last_mode", "not called yet") if self.nimble.configured else "NO API KEY"
                },
            },
            "demo_request": scenarios.DEMO_REQUEST,
        }


def build_app(hb: App | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal hb
        if hb is None:
            settings = Settings()
            store = Store(settings.db_path)
            analytics = Tinybird(settings.tinybird_host, settings.tinybird_token)
            for attempt in range(20):
                try:
                    await analytics.setup()
                    log.info("Tinybird live at %s", analytics.host)
                    break
                except Exception as e:
                    log.warning("Tinybird not ready (%s), attempt %d", e, attempt + 1)
                    await asyncio.sleep(3)
            else:
                log.error("Tinybird unreachable: using LOCAL FALLBACK analytics")
                analytics = LocalAnalytics()
            liquid = Liquid(settings.liquid_base_url, settings.liquid_model, settings.liquid_api_key)
            nimble = Nimble(settings.nimble_base_url, settings.nimble_api_key, settings.replay_dir)
            hb = App(settings, store, analytics, liquid, nimble, SandboxClient(settings.sandbox_url))
            hb.liquid_ok = await liquid.health()
            log.info("Liquid %s at %s (%s)", "live" if hb.liquid_ok else "UNREACHABLE", liquid.base_url, liquid.model)
        app.state.hb = hb
        app.state.store = hb.store
        app.state.inventory_lookup = hb.inventory
        await hb.reset()
        hb.watch_task = asyncio.create_task(hb.watcher.run())
        yield
        hb.watch_task.cancel()
        if hb.world:
            hb.world.stop()

    app = FastAPI(title="HorizonBook", lifespan=lifespan)
    app.include_router(sandbox.router)

    def _hb() -> App:
        return app.state.hb

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, **_hb().state()["integrations"]}

    @app.get("/api/state")
    def state() -> dict:
        return _hb().state()

    @app.post("/api/intent")
    async def post_intent(body: IntentIn) -> dict:
        hb = _hb()
        if hb.store.intent() or hb.runtime.status != "IDLE":
            raise HTTPException(409, "an intent is already owned; reset the demo first")
        hb.runtime.set_status("COMPILING")  # claims the slot before the background task starts
        if not hb.liquid_ok and hb.liquid:
            hb.liquid_ok = await hb.liquid.health()
        hb.spawn(hb.submit_intent(body.text))
        return {"accepted": True}

    @app.post("/api/world/price-drop")
    async def price_drop() -> dict:
        hb = _hb()
        assert hb.world
        return {"rows": await hb.world.fare_sale()}

    @app.post("/api/world/schedule-change")
    async def schedule_change() -> dict:
        hb = _hb()
        assert hb.world
        intent = hb.store.intent()
        booking = hb.store.active_booking(intent.id) if intent else None
        target = booking.candidate.flight_id if booking else "UA456"
        return {"rows": await hb.world.schedule_change(target)}

    @app.post("/api/demo/reset")
    async def reset(with_intent: bool = False) -> dict:
        hb = _hb()
        await hb.reset()
        if with_intent:
            hb.spawn(hb.submit_intent(scenarios.DEMO_REQUEST))
        return {"ok": True, "run_id": hb.store.get("run_id")}

    return app


app = build_app()
