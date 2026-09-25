"""Liquid AI: UNDERSTAND, DECIDE, AND REPLAN.

Talks to any OpenAI-compatible endpoint serving a Liquid LFM model (the local llama.cpp container by
default). Every call is metered; a call made while the agent is SLEEPING would be counted as
"tokens while waiting" -- by construction the watcher never does that.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger("horizonbook.liquid")


class LiquidError(RuntimeError):
    pass


class Liquid:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        on_usage: Callable[[dict], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.on_usage = on_usage
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=3.0))

    @property
    def label(self) -> str:
        return f"liquid:{self.model}"

    async def health(self) -> bool:
        try:
            r = await self._http.get(f"{self.base_url}/models", headers=self._headers())
            return r.status_code < 300
        except httpx.HTTPError:
            return False

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def chat_json(
        self, task: str, system: str, user: str, schema: dict[str, Any], max_tokens: int = 700
    ) -> dict[str, Any]:
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # llama.cpp compiles this schema to a grammar; hosted endpoints treat it as structured output.
            "response_format": {"type": "json_schema", "json_schema": {"name": task, "schema": schema, "strict": True}},
        }
        t0 = time.perf_counter()
        try:
            r = await self._http.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers())
        except httpx.HTTPError as e:
            raise LiquidError(f"{type(e).__name__}: {e}") from e
        if r.status_code >= 300:
            raise LiquidError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        message = data["choices"][0]["message"]
        content = message.get("content") or ""
        usage = data.get("usage") or {}
        meta = {
            "task": task,
            "model": data.get("model", self.model),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "finish_reason": data["choices"][0].get("finish_reason"),
        }
        if self.on_usage:
            self.on_usage(meta)
        parsed = _parse_json(content)
        parsed["_meta"] = meta
        return parsed


def _parse_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LiquidError(f"no JSON in model output: {text[:200]!r}") from None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LiquidError(f"invalid JSON in model output: {text[:200]!r}") from e
