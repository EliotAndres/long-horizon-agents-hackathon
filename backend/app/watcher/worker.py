"""Zero-LLM watcher. Asks Tinybird's condition endpoints whether anything that matters changed.

This loop is the only thing running while the agent sleeps. It never calls Liquid; it calls
``runtime.wake`` only when a Tinybird condition fires.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..agents.runtime import AgentRuntime
from ..store import Store
from . import conditions as cond

log = logging.getLogger("horizonbook.watcher")

# Plan-breaking conditions are evaluated first: repairing the plan beats chasing a bargain.
PRIORITY = {"arrival_after": 0, "availability_changed": 1, "price_below": 2, "better_candidate": 3}


class Watcher:
    def __init__(self, store: Store, analytics: Any, runtime: AgentRuntime, interval: float = 1.0):
        self.store, self.analytics, self.runtime, self.interval = store, analytics, runtime, interval
        self._nudge = asyncio.Event()

    def nudge(self) -> None:
        """Called after a world event is ingested, so evaluation happens immediately instead of next tick."""
        self._nudge.set()

    async def check_once(self) -> cond.Firing | None:
        if self.runtime.lock.locked() or self.runtime.status != "SLEEPING":
            return None
        if time.time() < self.store.get("retry_not_before", 0):
            return None
        intent = self.store.intent()
        run_id = self.store.get("run_id")
        if not intent or not run_id or intent.status == "COMPLETE":
            return None
        for c in sorted(self.store.conditions(intent.id), key=lambda c: PRIORITY[c.type]):
            pipe, params = cond.query_for(c, intent, run_id)
            rows = await self.analytics.endpoint(pipe, params)
            self.store.incr("tinybird_checks")
            if firing := cond.evaluate(c, rows):
                self.store.incr("tinybird_fires")
                return firing
        return None

    async def run(self) -> None:
        while True:
            try:
                if firing := await self.check_once():
                    await self.runtime.wake(firing)
            except Exception as e:  # never let the watcher die during a demo
                log.warning("watch cycle failed: %s", e)
                self.store.incr("tinybird_errors")
            try:
                await asyncio.wait_for(self._nudge.wait(), timeout=self.interval)
            except TimeoutError:
                pass
            self._nudge.clear()
