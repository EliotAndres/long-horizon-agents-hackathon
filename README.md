# long-horizon-agents-hackathon

A web agent for everyday tasks (groceries, restaurants, travel...). Describe the task in plain English. Nimble search finds the best websites, an on-device vision model (Liquid AI LFM2.5-VL-3B, run locally with MLX) browses it the way a person would, from screenshots only. The agent gets better from generic learnings that a separate agent writes. Traces stored and queried in RawTree.

```mermaid
flowchart TD
    Q["CLI: 'Find a vegan restaurant in the Mission and give me its address'"] --> A1
    subgraph A["Step A: pick a website"]
        A1[Nimble web search] --> A2[VLM picks the best result]
    end
    A2 --> L
    L["Step L: load learnings<br/>(if/then rules from RawTree)"] --> B1
    subgraph B["Step B: agent loop (headless Chromium)"]
        B1[B1 load page] --> B2["B2 screenshot + task + history + learnings<br/>→ VLM picks click / type / enter / scroll / done"]
        B2 --> B3["B3 act (clicks grounded by bounding box)"]
        B3 --> B4[B4 trace step to RawTree]
        B4 -- next step --> B2
    end
    B4 -- done --> R[answer + replay.mp4]
    B4 -. traces .-> LA[[learning agent]]
    LA -. writes learnings .-> L
```

**A. Pick a website.** The task goes to Nimble's search API. The local VLM reads the result titles and picks the site where the task can best be done.

**L. Load learnings.** The agent loads the newest generic rules from the RawTree table `v4_learnings`, for example "If a popup covers the page, close it before anything else". A separate learning agent writes these from past traces. They describe habits, never site-specific clicks, and are added to every action prompt.

**B. Agent loop.** It runs until the model says `done` or `--max-steps` is reached.
- **B1** loads the chosen page in headless Chromium (Playwright).
- **B2** takes a screenshot. The VLM picks the next action from the screenshot, the task, its past actions and the learnings.
- **B3** does the action. For a click, the VLM returns the target's bounding box and the agent clicks its centre. An action that leaves the screen unchanged is marked as such in the history.
- **B4** sends the step (thought, action, result, URL, latency) to RawTree. These traces are what the learning agent learns from.

## Run

```sh
cd browser-use-loop
uv sync && uv run playwright install chromium
NIMBLE_API_KEY=... RAWTREE_API_KEY=... uv run main.py "Find a vegan restaurant in the Mission in San Francisco and give me its address"
uv run main.py "..." --url https://www.instacart.com   # skip step A
uv run main.py "..." --learn                           # also reflect on this run into local lessons (below)
uv run python -m unittest discover -s tests            # tests, no model or network
```

| Env var | |
|---|---|
| `NIMBLE_API_KEY` (or `NIMBLE_KEY`) | step A search |
| `RAWTREE_API_KEY` | learnings and traces; both skipped if unset |
| `NIMBLE_PROXY` | optional, routes the browser through Nimble's proxy (for sites that block headless browsers) |

**Outputs:**
- `runs/<run_id>/`: one screenshot per step and `replay.mp4` (1.5 s per step, red circle on each click, the model's thought and action underneath).
- RawTree tables:
  - `v4_learnings` (read): `text`, `ts`.
  - Written: `v4_site_picks`, `v4_runs` (includes the learnings used), `v4_steps` and `v4_results` (and `v4_reflections` with `--learn`).

**Self-reflection (`--learn`, off by default).** Separate from step L. Ported from the archived agent's reflection loop (`learning.py`).
- Before the run, the 8 most recent active lessons from `runs/lessons.json` are appended to the action prompt.
- After the run, the VLM gets the task, the outcome, every action with its result (no-ops marked) and a grid of step screenshots. It returns `{"remove": [lesson ids], "lessons": ["If ..., then ..."]}`.
- Lessons it blames are deactivated, not deleted. New lessons are kept only if they are general: no digits, no capitalized names (cities, airport codes, sites), no words from the task's names, no dates, no URLs and no secrets. Lowercase names that are not in the task can still get through.
- A lesson's id is `sha256(normalized text)[:16]`, so it stays the same across runs.
- If the reflection fails, it is logged and the run is unaffected. Without `--learn`, the prompts and traces are unchanged.

With `--learn`, RawTree also gets `learning` and `active_lesson_ids` on `v4_runs`, plus `no_op_steps` on `v4_results`. Each `v4_steps` row gets `active_lesson_ids`, `no_op` and `model_ms` (model time per call kind: `policy`, `grounding`). One `v4_reflections` row per run records success, steps, no-op steps, the ids and texts of lessons added and removed, the latency and any error.

`archived/browser-use-loop` holds the earlier MiniWoB flight-booking agent, which has a reflection and lessons loop and Tinybird telemetry.
