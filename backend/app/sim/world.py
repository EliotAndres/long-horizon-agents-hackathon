"""Runs the simulated world: background market noise, the 48h time-lapse, and external world events.

Everything the world does reaches the agent only through Tinybird observations.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from ..store import Store
from .market import Market

log = logging.getLogger("horizonbook.world")


class World:
    def __init__(
        self, store: Store, analytics: Any, market: Market, tick_interval: float, on_change: Callable[[], None]
    ):
        self.store, self.analytics, self.market = store, analytics, market
        self.tick_interval, self.on_change = tick_interval, on_change
        self.task: asyncio.Task | None = None
        self.phase = "idle"

    async def publish(self, rows: list[dict], wait: bool = True) -> None:
        await self.analytics.ingest(rows, wait=wait)
        self.store.incr("market_observations", len(rows))
        self.store.set("market_changes", self.market.changes)
        self.store.set("sim_clock", self.market.clock.isoformat())

    async def seed(self) -> None:
        await self.publish(self.market.snapshot())

    def start(self, timelapse: bool) -> None:
        self.stop()
        self.task = asyncio.create_task(self._run(timelapse))

    def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None

    async def _run(self, timelapse: bool) -> None:
        try:
            if timelapse:
                self.phase = "timelapse"
                self.store.log("WORLD", "world", "Fast-forwarding 48 simulated hours of market movement")
                for batch in self.market.timelapse():
                    await self.publish(batch, wait=False)
                    self.on_change()
                    await asyncio.sleep(0.4)
                await self.publish(self.market.snapshot())
                intent = self.store.intent()
                quiet = bool(intent) and intent.status == "MONITORING" and not self.store.metrics().get("wakes")
                self.store.log(
                    "WORLD",
                    "world",
                    f"48h elapsed: {self.market.changes} price changes"
                    + (", none satisfied the intent" if quiet else ""),
                )
            self.phase = "live"
            while True:
                await asyncio.sleep(self.tick_interval)
                await self.publish(self.market.tick(), wait=False)
                self.on_change()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("world loop crashed")
            self.phase = "crashed"

    async def fare_sale(self) -> list[dict]:
        rows = self.market.fare_sale()
        await self.publish(rows)
        self.store.log(
            "WORLD",
            "world",
            "External event: fare sale. " + ", ".join(f"{r['flight_id']} now ${r['price']:.0f}" for r in rows),
            {"rows": rows},
        )
        self.on_change()
        return rows

    async def schedule_change(self, flight_id: str) -> list[dict]:
        before = next(v for v in self.market.view() if v["flight_id"] == flight_id)
        rows = self.market.schedule_change(flight_id)
        await self.publish(rows)
        r = rows[0]
        self.store.log(
            "WORLD",
            "world",
            f"External event: airline retimed {flight_id}: arrival "
            f"{before['arrival']} -> {r['arrival']} (departs {r['departure']})",
            {"rows": rows},
        )
        self.on_change()
        return rows
