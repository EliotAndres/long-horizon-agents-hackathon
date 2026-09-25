"""Tinybird: KNOW WHEN SOMETHING IMPORTANT CHANGES.

Market observations stream into the `market_observations` data source through the Events API.
Watch conditions are evaluated by the deployed endpoint pipes (see /tinybird/endpoints); the
backend only passes validated parameters, never generated SQL.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger("horizonbook.tinybird")

DATASOURCE = "market_observations"


class TinybirdError(RuntimeError):
    pass


class Tinybird:
    name = "tinybird"

    def __init__(self, host: str, token: str = ""):
        self.host = host.rstrip("/")
        self.token = token
        self.live = False
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))

    @property
    def is_local(self) -> bool:
        return "localhost" in self.host or "tinybird-local" in self.host or "127.0.0.1" in self.host

    async def setup(self) -> None:
        if not self.token and self.is_local:
            r = await self._http.get(f"{self.host}/tokens")
            r.raise_for_status()
            self.token = r.json()["workspace_admin_token"]
        if not self.token:
            raise TinybirdError("TINYBIRD_TOKEN is not set")
        # Proves the deployment exists: the endpoint must answer for a run that has no data yet.
        await self.endpoint("market_stats", {"run_id": "healthcheck"})
        self.live = True

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def ingest(self, rows: list[dict], wait: bool = True) -> None:
        """wait=True returns only once rows are queryable (world events); False for bulk background noise."""
        if not rows:
            return
        body = "\n".join(json.dumps(r) for r in rows)
        r = await self._http.post(
            f"{self.host}/v0/events",
            params={"name": DATASOURCE, "wait": "true" if wait else "false"},
            content=body.encode(),
            headers=self._headers(),
        )
        if r.status_code >= 300:
            raise TinybirdError(f"events API {r.status_code}: {r.text[:300]}")
        quarantined = r.json().get("quarantined_rows", 0) if r.content else 0
        if quarantined:
            raise TinybirdError(f"{quarantined} rows quarantined")

    async def endpoint(self, pipe: str, params: dict[str, Any]) -> list[dict]:
        q = {k: (",".join(v) if isinstance(v, list) else v) for k, v in params.items() if v is not None}
        r = await self._http.get(f"{self.host}/v0/pipes/{pipe}.json", params=q, headers=self._headers())
        if r.status_code >= 300:
            raise TinybirdError(f"pipe {pipe} {r.status_code}: {r.text[:300]}")
        return r.json()["data"]


class LocalAnalytics:
    """Emergency stand-in with the same endpoint semantics, used only if Tinybird is unreachable.

    The dashboard labels it loudly; the demo is meant to run on Tinybird.
    """

    name = "local-fallback"
    live = False

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def setup(self) -> None:
        return None

    async def ingest(self, rows: list[dict], wait: bool = True) -> None:
        self.rows.extend(rows)

    def _latest(self, run_id: str) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for r in self.rows:
            if r["run_id"] == run_id and (r["flight_id"] not in latest or r["seq"] > latest[r["flight_id"]]["seq"]):
                latest[r["flight_id"]] = r
        return latest

    async def endpoint(self, pipe: str, params: dict[str, Any]) -> list[dict]:
        latest = self._latest(params["run_id"])
        if pipe == "market_stats":
            n = sum(1 for r in self.rows if r["run_id"] == params["run_id"])
            return [{"observations": n, "flights": len(latest)}]
        if pipe == "qualifying_candidates":
            out = [
                {**r, "last_seq": r["seq"]}
                for r in latest.values()
                if r["available"] == 1
                and r["travel_date"] == params["travel_date"]
                and r["destination"] == params.get("destination", "LAX")
                and r["origin"] in params["origins"]
                and r["price"] <= float(params["max_price"])
                and r["arrival_min"] <= int(params["arrival_before_min"])
                and r["departure_min"] >= int(params["departure_after_min"])
                and r["flight_id"] != params.get("exclude_flight")
            ]
            return sorted(out, key=lambda r: (r["price"], r["arrival_min"]))
        if pipe == "plan_health":
            r = latest.get(params["flight_id"])
            if not r:
                return []
            return [
                {
                    **r,
                    "last_seq": r["seq"],
                    "arrival_violated": int(r["arrival_min"] > int(params["arrival_before_min"])),
                    "departure_violated": int(r["departure_min"] < int(params["departure_after_min"])),
                    "unavailable": int(r["available"] == 0),
                }
            ]
        raise ValueError(pipe)
