"""Plan switch: secure the replacement seat first, then release the old booking.

Never leaves the traveler with nothing: if the new booking fails, the old one is untouched.
"""

from __future__ import annotations

from ..booking.sandbox import SandboxClient
from ..models import ActiveBooking, CandidateFlight, TravelIntent
from ..store import Store, now_iso
from .planner import Decision


async def book(
    sandbox: SandboxClient, store: Store, intent: TravelIntent, cand: CandidateFlight, decision: Decision, wake_id: str
) -> ActiveBooking:
    conf = await sandbox.book(cand.flight_id, cand.price, idempotency_key=f"{intent.id}:{wake_id}:{cand.flight_id}")
    booked = cand.model_copy(update={"price": float(conf["price"])})
    b = ActiveBooking(
        booking_id=conf["booking_id"],
        intent_id=intent.id,
        candidate=booked,
        created_at=now_iso(),
        justified_by=decision.justified_by,
        reasoning=decision.reasoning,
        decided_by=decision.decided_by,
    )
    store.put_booking(b)
    return b


async def switch(
    sandbox: SandboxClient,
    store: Store,
    intent: TravelIntent,
    old: ActiveBooking,
    cand: CandidateFlight,
    decision: Decision,
    wake_id: str,
) -> tuple[ActiveBooking, dict]:
    new = await book(sandbox, store, intent, cand, decision, wake_id)
    cancelled = await sandbox.cancel(old.booking_id)
    old.status = "CANCELLED"
    store.put_booking(old)
    return new, cancelled
