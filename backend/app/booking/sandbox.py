"""Local sandbox booking provider. Stands in for an airline/OTA booking API. No real money moves.

The provider is exposed over HTTP (``/sandbox/...``) and the agent calls it over HTTP, exactly as it
would call a real provider. Bookings are idempotent per ``idempotency_key``.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..store import Store, now_iso

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


class BookingRequest(BaseModel):
    flight_id: str
    quoted_price: float
    passenger: str = "Demo Traveler"
    idempotency_key: str


def _store(request: Request) -> Store:
    return request.app.state.store


def _next_id(store: Store) -> str:
    n = int(store.get("sandbox_seq", 0))
    store.set("sandbox_seq", n + 1)
    return f"HX-{48321 if n == 0 else (48321 + n * 7919) % 90000 + 10000}"


@router.post("/bookings")
def create_booking(req: BookingRequest, request: Request) -> dict:
    store = _store(request)
    if prior := store.sandbox_get(idem=req.idempotency_key):
        return prior
    # The provider checks the live world, like a real inventory system would.
    lookup: Callable[[str], dict | None] = request.app.state.inventory_lookup
    flight = lookup(req.flight_id)
    if not flight:
        raise HTTPException(404, f"unknown flight {req.flight_id}")
    if not flight["available"]:
        raise HTTPException(409, f"{req.flight_id} is sold out")
    # Fare hold: the quoted fare is honoured unless the live fare has since risen by more than $5.
    if flight["price"] > req.quoted_price + 5:
        raise HTTPException(409, f"fare changed: quoted ${req.quoted_price:.0f}, now ${flight['price']:.0f}")
    body = {
        "booking_id": _next_id(store),
        "flight_id": req.flight_id,
        "status": "CONFIRMED",
        "price": req.quoted_price,
        "passenger": req.passenger,
        "created_at": now_iso(),
    }
    store.sandbox_put(body["booking_id"], req.idempotency_key, "CONFIRMED", body)
    return body


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, request: Request) -> dict:
    store = _store(request)
    body = store.sandbox_get(booking_id=booking_id)
    if not body:
        raise HTTPException(404, f"unknown booking {booking_id}")
    body.update(status="CANCELLED", cancelled_at=now_iso())
    store.sandbox_put(booking_id, None, "CANCELLED", body)
    return body


@router.get("/bookings")
def list_bookings(request: Request) -> list[dict]:
    return _store(request).sandbox_all()


class SandboxClient:
    """What the agent uses. Plain HTTP to the provider."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=10.0)

    async def book(self, flight_id: str, quoted_price: float, idempotency_key: str) -> dict:
        r = await self._http.post(
            "/sandbox/bookings",
            json={"flight_id": flight_id, "quoted_price": quoted_price, "idempotency_key": idempotency_key},
        )
        r.raise_for_status()
        return r.json()

    async def cancel(self, booking_id: str) -> dict:
        r = await self._http.post(f"/sandbox/bookings/{booking_id}/cancel")
        r.raise_for_status()
        return r.json()
