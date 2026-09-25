# TODO

## MUST WORK
- [x] Persistent TravelIntent compiled by Liquid (LFM2.5-1.2B, schema-constrained JSON)
- [x] Market observations streamed to Tinybird (Events API); conditions as deployed endpoint pipes
- [x] Zero-LLM watcher; Liquid tokens while waiting = 0 (metered)
- [x] Price drop -> Tinybird fires -> automatic wake -> Liquid selects -> sandbox booking
- [x] Schedule change -> Tinybird plan_health fires -> Liquid replans -> book replacement, cancel old -> re-arm -> sleep
- [x] Full stack on `docker compose up` (tinybird-local, tb deploy, llama.cpp Liquid, backend, frontend)
- [ ] **Genuine Nimble call: set NIMBLE_API_KEY in .env and verify one live search appears as LIVE WEB**

## DEMO
- [x] 16:9 dashboard: status, zero-token counter, intent, market, tool lanes, timeline, world-event controls
- [x] Reset script (`scripts/reset-demo.sh`) with fresh Tinybird run id
- [ ] Record a successful live Nimble response (auto-saved to data/replay/) as backup before recording
- [ ] Rehearse the 3-minute script in README; record

## OPTIONAL
- [ ] Tinybird Cloud workspace instead of Local (set TINYBIRD_HOST/TINYBIRD_TOKEN)
- [ ] Hosted/larger Liquid model if the event provides an endpoint (better multi-criteria ranking)
- [ ] Intent COMPLETE when the trip date passes
- [ ] Nimble extract of a specific fare page to feed real prices into observations
