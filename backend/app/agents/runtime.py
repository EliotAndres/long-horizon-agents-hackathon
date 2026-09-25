"""The agent's wake cycle. Invoked only by the watcher when a Tinybird condition fires.

SLEEPING -> WAKING/REPLANNING -> (Tinybird candidates) -> Nimble verify -> Liquid decide
         -> sandbox book / switch -> arm new watch conditions -> SLEEPING
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import date
from typing import Any

from pydantic import ValidationError

from ..booking.sandbox import SandboxClient
from ..integrations.liquid import Liquid, LiquidError
from ..integrations.nimble import Nimble, mentions
from ..models import TravelIntent
from ..store import Store
from ..watcher import conditions as cond
from . import context as ctxmod
from . import intent as intentmod
from . import planner, replan

log = logging.getLogger("horizonbook.agent")

INVALIDATING = {"arrival_after", "availability_changed"}
RETRY_BACKOFF_S = 5.0


class AgentRuntime:
    def __init__(
        self,
        store: Store,
        analytics: Any,
        liquid: Liquid | None,
        nimble: Nimble,
        sandbox: SandboxClient,
        step_delay: float = 0.0,
    ):
        self.store, self.analytics, self.liquid, self.nimble, self.sandbox = store, analytics, liquid, nimble, sandbox
        self.step_delay = step_delay
        self.lock = asyncio.Lock()

    # --- status -------------------------------------------------------------------------------
    @property
    def status(self) -> str:
        return self.store.get("agent_status", "IDLE")

    def set_status(self, s: str) -> None:
        self.store.set("agent_status", s)

    def on_liquid_usage(self, meta: dict) -> None:
        """Metering hook for every Liquid call. A call while SLEEPING would show up as waiting tokens."""
        self.store.incr("liquid_calls")
        self.store.incr("liquid_tokens", meta.get("total_tokens", 0))
        if self.status == "SLEEPING":
            self.store.incr("liquid_tokens_while_waiting", meta.get("total_tokens", 0))

    async def _pause(self) -> None:
        if self.step_delay:
            await asyncio.sleep(self.step_delay)

    def _sleep(self, intent: TravelIntent) -> None:
        armed = self.store.conditions(intent.id)
        self.set_status("SLEEPING")
        self.store.set(
            "sleep_started_at",
            self.store.log(
                "SLEEP",
                "agent",
                "Agent sleeping. Liquid idle; Tinybird is watching.",
                {"armed": [c.type for c in armed]},
            ).timestamp,
        )

    def _arm(
        self, intent: TravelIntent, booking, proposed: list[dict] | None, by: str, fired: cond.Firing | None = None
    ) -> None:
        conds = cond.build_policy(intent, booking, proposed, by)
        for c in conds:
            # The evidence that just woke us must not wake us again: carry its fingerprint forward.
            if fired and c.type == fired.condition.type:
                c.last_fingerprint = fired.fingerprint
        self.store.replace_conditions(intent.id, conds)
        self.store.log(
            "ARM",
            "tinybird",
            f"Armed {len(conds)} watch condition(s) on Tinybird endpoints",
            {"conditions": [{"type": c.type, "by": c.created_by, "rule": cond.describe(c, intent)} for c in conds]},
        )

    # --- 1. user intent ---------------------------------------------------------------------------
    async def accept_intent(self, text: str, today: date) -> TravelIntent:
        async with self.lock:
            self.set_status("COMPILING")
            self.store.log("USER_INTENT", "user", text)
            intent = None
            if self.liquid:
                try:
                    intent, meta = await intentmod.compile_intent(self.liquid, text, today)
                    self.store.log("LIQUID", "liquid", "Compiled request into durable TravelIntent", meta)
                except (LiquidError, ValidationError, KeyError) as e:
                    self.store.log(
                        "GUARDRAIL", "liquid", f"Liquid intent compile unusable ({e}); deterministic parser used"
                    )
            if intent is None:
                intent = intentmod.fallback_intent(text, today)
            self.store.put_intent(intent)
            self.store.log(
                "INTENT",
                "agent",
                "Persistent intent stored. The chat is no longer needed.",
                {"intent": intent.model_dump()},
            )
            await self._pause()

            # 2. initial discovery on the live web
            self.set_status("DISCOVERING")
            await self._nimble(
                f"flights {' '.join(intent.hard_constraints.allowed_origins)} to {intent.destination} "
                f"{date.fromisoformat(intent.travel_date).strftime('%B %d %Y')} nonstop",
                "Initial discovery",
            )
            await self._pause()

            # 3. wait
            self._arm(intent, None, None, "policy")
            self._sleep(intent)
            return intent

    # --- 4-8. wake cycle -------------------------------------------------------------------------
    async def wake(self, firing: cond.Firing) -> None:
        async with self.lock:
            try:
                await self._wake(firing)
            except Exception as e:  # keep the agent alive and let the same condition fire again shortly
                log.exception("wake failed")
                self.store.set("retry_not_before", time.time() + RETRY_BACKOFF_S)
                self.store.log(
                    "ERROR", "agent", f"Wake cycle failed ({type(e).__name__}: {e}); retrying in {RETRY_BACKOFF_S:.0f}s"
                )
                intent = self.store.intent()
                if intent:
                    self._sleep(intent)

    async def _wake(self, firing: cond.Firing) -> None:
        intent = self.store.intent()
        if not intent:
            return
        c = firing.condition
        wake_id = uuid.uuid4().hex[:6]
        self.store.incr("wakes")
        booking = self.store.active_booking(intent.id)
        if c.type in INVALIDATING and (not booking or booking.candidate.flight_id != c.parameters.get("flight_id")):
            # Stale guard on a flight we no longer hold: re-arm for the current plan, no LLM needed.
            self._arm(intent, booking if booking and booking.status == "ACTIVE" else None, None, "policy")
            self._sleep(intent)
            return
        invalidating = c.type in INVALIDATING

        self.store.log(
            "TRIGGER",
            "tinybird",
            ("ACTIVE PLAN INVALID: " if invalidating else "OPPORTUNITY DETECTED: ")
            + f"{cond.describe(c, intent)} -> {firing.summary}",
            {"condition": c.type, "rows": firing.rows},
        )
        if invalidating and booking:
            booking.status = "INVALID"
            self.store.put_booking(booking)
            intent.status = "REPLANNING"
            self.store.put_intent(intent)
            self.set_status("REPLANNING")
        else:
            self.set_status("WAKING")
        self.store.log("WAKE", "agent", f"Agent waking (wake {wake_id}). Loading durable intent, not chat history.")
        await self._pause()

        # Relevant current candidates only: ask Tinybird, excluding a flight that just broke the plan.
        run_id = self.store.get("run_id")
        pipe, params = cond.qualifying_query(intent, run_id)
        if invalidating and booking:
            params["exclude_flight"] = booking.candidate.flight_id
        rows = await self.analytics.endpoint(pipe, params)
        self.store.incr("tinybird_checks")
        candidates = [ctxmod.candidate_from_row(r) for r in rows][:5]
        self.store.log(
            "CANDIDATES",
            "tinybird",
            f"{len(candidates)} candidate(s) satisfy every hard constraint",
            {"candidates": [x.model_dump() for x in candidates]},
        )

        web = None
        if candidates:
            carriers = sorted({x.carrier for x in candidates})
            origins = sorted({x.origin for x in candidates})
            web = await self._nimble(
                f"{' '.join(carriers)} nonstop flights {' '.join(origins)} to {intent.destination}",
                "Live option check" if not invalidating else "Checking live alternatives",
            )
            web["per_flight"] = {
                x.flight_id: sum(1 for r in web["results"] if mentions(r, x.carrier)) for x in candidates
            }
        await self._pause()

        trigger = {"type": c.type, "summary": firing.summary, "plan_invalid": invalidating}
        decision = await planner.choose(self.liquid, intent, trigger, candidates, booking, web)
        self.store.log(
            "LIQUID" if decision.decided_by.startswith("liquid") else "GUARDRAIL",
            "liquid",
            (
                f"{'Replanned' if invalidating else 'Decided'}: {decision.action} {decision.candidate.flight_id}"
                if decision.candidate
                else f"Decided: {decision.action}"
            )
            + (f" -- {decision.reasoning}" if decision.reasoning else ""),
            {"decided_by": decision.decided_by, "guardrail": decision.guardrail, **decision.meta},
        )
        await self._pause()

        if (
            decision.action == "BOOK"
            and decision.candidate
            and booking
            and booking.status == "ACTIVE"
            and booking.candidate.flight_id == decision.candidate.flight_id
        ):
            decision.action = "KEEP"  # already holding this seat; never double-book it

        if decision.action == "BOOK" and decision.candidate:
            self.set_status("BOOKING")
            cand = decision.candidate
            old = booking if booking and booking.candidate.flight_id != cand.flight_id else None
            if old:
                new, cancelled = await replan.switch(self.sandbox, self.store, intent, old, cand, decision, wake_id)
            else:
                new, cancelled = await replan.book(self.sandbox, self.store, intent, cand, decision, wake_id), {}
            self.store.log(
                "BOOKED",
                "sandbox",
                f"{'Replacement' if old else 'Booking'} {new.booking_id} confirmed: {cand.flight_id} "
                f"{cand.origin}->{cand.destination} {cand.departure}->{cand.arrival} ${new.candidate.price:.0f}",
                {"booking": new.model_dump()},
            )
            if old:
                self.store.log(
                    "CANCELLED",
                    "sandbox",
                    f"Old booking {cancelled['booking_id']} ({old.candidate.flight_id}) cancelled",
                    {"cancelled": cancelled},
                )
                self.store.incr("cancellations")
                self.store.log(
                    "REBOOKED",
                    "agent",
                    f"Plan repaired: {old.candidate.flight_id} -> {cand.flight_id} "
                    "(new seat secured before the old one was released)",
                )
            self.store.incr("bookings")
            intent.status = "BOOKED"
            self.store.put_intent(intent)
            await self._pause()
            self._arm(intent, new, decision.watch, decision.decided_by, firing)
        elif booking and booking.status == "ACTIVE":
            # KEEP (or WAIT) while holding a valid seat: keep guarding it.
            self._arm(intent, booking, decision.watch, decision.decided_by, firing)
        else:
            # Nothing acceptable right now: keep owning the intent and watch for an opportunity.
            intent.status = "MONITORING"
            self.store.put_intent(intent)
            self._arm(intent, None, None, "policy", firing)
        self._sleep(intent)

    async def _nimble(self, query: str, purpose: str) -> dict:
        self.store.incr("nimble_calls")
        res = await self.nimble.search(query)
        if res["mode"] == "live":
            self.store.incr("nimble_live")
        self.store.set("nimble_last_mode", res["mode"])
        n = len(res["results"])
        fares = res.get("fares") or []
        label = {"live": "LIVE WEB", "replay": "REPLAY (recorded live response)", "unavailable": "UNAVAILABLE"}[
            res["mode"]
        ]
        msg = f"{purpose}: {n} current web source(s) [{label}]"
        if fares:
            msg += f"; web fares seen ${fares[0]}-${fares[-1]}"
        if res["mode"] == "unavailable":
            msg += f" ({res.get('error', '')})"
        self.store.log("NIMBLE", "nimble", msg, {"query": query, **res})
        return res
