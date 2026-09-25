"""Nimble: SEE THE CURRENT WEB.

Genuine live web search through Nimble's SDK API. Successful responses are recorded to disk so a
network failure during judging degrades to a clearly labelled replay instead of a broken demo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("horizonbook.nimble")

PRICE = re.compile(r"\$\s?(\d{2,4})(?:\.\d{2})?")


class Nimble:
    def __init__(self, base_url: str, api_key: str, replay_dir: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.replay_dir = Path(replay_dir)
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=5.0))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _replay_path(self, query: str) -> Path:
        return self.replay_dir / f"nimble-{hashlib.sha1(query.encode(), usedforsecurity=False).hexdigest()[:10]}.json"

    async def search(self, query: str, max_results: int = 6) -> dict[str, Any]:
        """Returns {mode: live|replay|unavailable, query, results:[{title,url,snippet}], fares:[...], latency_ms}."""
        t0 = time.perf_counter()
        error = "NIMBLE_API_KEY not set"
        if self.api_key:
            try:
                r = await self._http.post(
                    f"{self.base_url}/search",
                    json={"query": query, "max_results": max_results, "country": "US", "locale": "en-US"},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                if r.status_code < 300:
                    out = self._normalize(query, r.json(), "live")
                    out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
                    self._record(query, out)
                    return out
                error = f"HTTP {r.status_code}: {r.text[:200]}"
            except (httpx.HTTPError, ValueError) as e:  # ValueError: non-JSON body
                error = f"{type(e).__name__}: {e}"
        log.warning("nimble search failed (%s); trying replay", error)
        path = self._replay_path(query)
        if path.exists():
            out = json.loads(path.read_text())
            out.update(mode="replay", error=error, latency_ms=round((time.perf_counter() - t0) * 1000))
            return out
        return {"mode": "unavailable", "query": query, "results": [], "fares": [], "error": error, "latency_ms": 0}

    def _normalize(self, query: str, raw: dict, mode: str) -> dict[str, Any]:
        items = raw.get("results") or raw.get("organic_results") or raw.get("data") or []
        if isinstance(items, dict):  # some responses nest the list one level down
            items = items.get("results") or items.get("organic") or []
        results = []
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get(k) or "") for k in ("title", "description", "content"))
            results.append(
                {
                    "title": (item.get("title") or "")[:140],
                    "url": item.get("url") or item.get("link") or "",
                    "snippet": str(item.get("description") or item.get("snippet") or item.get("content") or "")[:280],
                    "fares": sorted({int(p) for p in PRICE.findall(text) if 30 <= int(p) <= 2000})[:6],
                }
            )
        fares = sorted({f for r in results for f in r["fares"]})
        return {
            "mode": mode,
            "query": query,
            "request_id": raw.get("request_id", ""),
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results": results,
            "fares": fares,
        }

    def _record(self, query: str, out: dict) -> None:
        try:
            self.replay_dir.mkdir(parents=True, exist_ok=True)
            self._replay_path(query).write_text(json.dumps(out, indent=1))
        except OSError as e:
            log.warning("could not record nimble replay: %s", e)


def mentions(result: dict, *terms: str) -> bool:
    blob = f"{result.get('title', '')} {result.get('snippet', '')} {result.get('url', '')}".lower()
    return all(t.lower() in blob for t in terms)
