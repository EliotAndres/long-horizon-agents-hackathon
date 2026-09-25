# HorizonBook

**An agent that owns the trip, not the chat.**

Most booking assistants search once and disappear. HorizonBook takes a travel intent once, turns it into
durable structured state, and then *owns* it until the trip happens: it sleeps while the world changes,
wakes only when something that matters changes, books, keeps watching the booking, and repairs the plan
when the world breaks it.

**Persistent intent, not persistent chat history. Event-driven wakeups, not an LLM polling loop.**

## Who does what

| Sponsor tool | Role | Where it is essential |
|---|---|---|
| **Nimble** | *What does the web say right now?* | Live web search at initial discovery and on every wake, to verify the candidate carriers/route (`backend/app/integrations/nimble.py`) |
| **Tinybird** | *When should the agent wake?* | Every market observation streams into `market_observations` (Events API). Watch conditions are **deployed endpoint pipes** with template parameters (`tinybird/endpoints/*.pipe`). The watcher only calls these endpoints; it never calls the LLM |
| **Liquid AI** | *What should I believe / choose / do now?* | LFM2.5 compiles the request into a `TravelIntent`, chooses among candidates, replans after invalidation, and writes the post-booking watch policy (`backend/app/agents/`) |

```
 user request ──▶ Liquid: compile intent ──▶ SQLite: TravelIntent (durable)      ──▶ Nimble: live web discovery
                                                                                   │
 world (fares, schedules) ──▶ Tinybird Events API ──▶ market_observations          ▼
                                      │                                      arm watch conditions
                                      ▼                                            │
                    watcher (no LLM) ──▶ Tinybird endpoints: qualifying_candidates / plan_health
                                      │ fired?
                                      ▼
            WAKE: load intent + trigger + relevant candidates (not chat) ──▶ Nimble verify ──▶ Liquid decide
                 ──▶ guardrail (hard constraints) ──▶ sandbox book / (book new, then cancel old)
                 ──▶ Liquid-proposed watch policy validated against templates ──▶ SLEEP
```

State the system keeps apart:

- **World state**: what exists now (the market simulator, observed via Tinybird).
- **User intent**: what must remain true (`TravelIntent`, hard constraints + preferences).
- **Active plan**: what the agent chose (`ActiveBooking` with `justified_by`).
- **Watch conditions**: what should wake it (`WatchCondition`, one Tinybird endpoint each).

Watch-condition templates (Liquid picks and parameterises; `watcher/conditions.py` validates):

| Template | Tinybird endpoint | Fires when |
|---|---|---|
| `price_below` | `qualifying_candidates` | a flight satisfies every hard constraint |
| `better_candidate` | `qualifying_candidates` (`max_price = booked - min_savings`, `exclude_flight`) | a materially cheaper qualifying flight appears |
| `arrival_after` | `plan_health` | the booked flight now lands after the deadline (or departs too early) |
| `availability_changed` | `plan_health` | the booked flight is no longer available |

## Run it

```bash
cp .env.example .env          # add NIMBLE_API_KEY (required for the live-web step)
docker compose up --build     # tinybird-local, tinybird-deploy (tb deploy), liquid (llama.cpp + LFM2.5), backend, frontend
open http://localhost:5173
```

First start downloads the LFM2.5-1.2B-Instruct GGUF (~1.2 GB) and the Tinybird Local image.
Reset between takes: `scripts/reset-demo.sh` (or the Reset button). `scripts/reset-demo.sh --intent` also submits the demo request.

Tinybird Cloud instead of Local: set `TINYBIRD_HOST` + `TINYBIRD_TOKEN` in `.env`; `tinybird-deploy` runs `tb deploy` there.
A hosted Liquid endpoint instead of the local model: set `LIQUID_BASE_URL` / `LIQUID_API_KEY` / `LIQUID_MODEL`.

Backend tests (offline, fakes for Tinybird and Liquid): `cd backend && uv run pytest`.

## The demo (3 minutes)

The only human inputs are the trip request, once, and two **world events**. There is no
"run agent", "replan" or "book" button anywhere.

1. **Hand over the trip.** Liquid compiles it; the intent panel shows the durable state; Nimble does live discovery;
   Tinybird arms `price_below`. The agent sleeps, and 48 simulated hours of market movement stream into Tinybird
   (~4,000 observations, ~3,600 price changes). *Liquid tokens while waiting: 0.*
2. **World event: fare sale.** UA456 $211 → $169. Then do nothing. Tinybird fires *opportunity detected* → agent wakes →
   Nimble checks the live web → Liquid picks UA456 over WN1402 → sandbox booking `HX-48321` → Liquid arms
   arrival/availability/better-fare conditions → asleep.
3. **World event: the airline retimes UA456** (arrival 08:12 → 10:05). Do nothing. Tinybird fires *active plan invalid* →
   Liquid wakes with the invalid booking + only the alternatives Tinybird returns → Nimble checks → replacement WN1402 booked,
   then UA456 cancelled → new conditions on WN1402 → asleep.

Pitch lines: *"Nimble sees the web. Tinybird knows when the world matters. Liquid decides what to do.
The agent owns the goal, not the conversation."*

## Honest boundaries

- Flight inventory is a **deterministic local market simulator** (`backend/app/sim/`), because live airline fare
  extraction is too unstable to stake a live demo on. Nimble calls are **genuine** live web searches about the route and
  carriers; each successful response is recorded to `data/replay/` and the UI labels any replayed response as such.
- Booking is a **local sandbox** provider (`/sandbox/bookings`), called over HTTP. No money moves.
- The default Liquid model is the 1.2B LFM2.5 running on CPU, for reliability. A deterministic guardrail enforces the
  user's hard constraints and auto-book policy on Liquid's output; if it overrides Liquid, the UI shows *Guardrail*.
- If Tinybird is unreachable at startup the backend falls back to an in-process evaluator and the header says
  **LOCAL FALLBACK** in red. The demo is meant to run on Tinybird.
