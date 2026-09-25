# v4 — question → Nimble picks the site → on-device VLM browses it

```
uv sync && uv run playwright install chromium
NIMBLE_API_KEY=... RAWTREE_API_KEY=... uv run main.py "Give me the cheapest flights from LA to SF on the 26th of September"
uv run main.py "..." --url https://www.kayak.com/flights   # skip step A
```

- **A** Nimble `/v2/search` on the question; LFM2.5-VL-3B (text-only) picks the result to open.
- **B** Headless Chromium loop: B1 load → B2 screenshot → VLM action (click/type/enter/scroll/done, clicks grounded by bbox) → B3 act → B4 trace.
- Traces go to RawTree tables `v4_site_picks`, `v4_runs`, `v4_steps` and `v4_results`. They are skipped if `RAWTREE_API_KEY` is unset. Screenshots are in `runs/<run_id>/`.
- `NIMBLE_PROXY` (optional) routes the browser through Nimble's residential proxy, for bot walls.
