# Tinybird telemetry for the browser agent

`agent.py` can send its traces to [Tinybird](https://www.tinybird.co) so you can check, with numbers,
whether the agent is actually improving: success rate, steps to success, dead actions, failing-action
hotspots, model latency, a learning curve per episode, and how outcomes correlate with each lesson.

It is optional and additive. `runs/trajectories.jsonl`, `runs/frames/`, `runs/lessons.json` and
`report.py` work exactly as before, with or without Tinybird.

```
agent.py ──> telemetry.py ──(Events API, NDJSON)──> datasources/ ──> endpoints/
```

## Turn it on

```sh
export TINYBIRD_TELEMETRY_ENABLED=true
export TINYBIRD_TOKEN=...                      # the append-only token below, never an admin token
export TINYBIRD_HOST=https://api.tinybird.co   # your workspace's region; http://localhost:7181 for Tinybird Local
uv run agent.py --episodes 20 --learn
```

Telemetry stays off unless `TINYBIRD_TELEMETRY_ENABLED` is true and `TINYBIRD_TOKEN` is set. The host
must be `https://`; `http://` is only accepted for localhost. Status lines go to stderr, prefixed with
`[telemetry]`.

## Deploy this project

```sh
cd browser-use-loop/tinybird
tb login                                        # writes .tinyb, which is gitignored
tb --cloud deploy
tb --cloud token copy agent_telemetry_append    # DATASOURCES:APPEND only: give this one to the agent
tb --cloud token copy agent_analytics_read      # PIPES:READ only: for querying the endpoints
```

For local development, run `tb local start` and then `tb build` in this folder.

## Events and datasources

| event (`telemetry.py`) | datasource | one row per |
|---|---|---|
| `run_started` | `agent_runs` | run: model, `learning_enabled`, temperature, seed, episodes, max steps |
| `episode_started` | `agent_episodes` | episode: seed, task, known lesson count, the ids and texts of the lessons in the prompt |
| `agent_step` | `agent_steps` | step: model state, action type/arg/result, `executed`, `screen_changed`, latency, screenshot reference |
| `model_call` | `model_calls` | VLM call (`policy`, `grounding`, `reflection`): redacted prompt hash and preview, structured output, latency, tokens |
| `episode_result` | `episode_results` | episode: reward, success, steps, no-change steps, duration, active lesson ids |
| `reflection_result` | `reflection_events` | reflection (`--learn`): lessons before, added, removed and after |
| `error` | `agent_errors` | failed model call, aborted run (including Ctrl-C), or a Tinybird send that gave up |

Every row also carries `run_id` (the same run id as `trajectories.jsonl`), `event_id` and a UTC `timestamp`.

## Endpoints

All of them accept `run_id`, `model`, `learning_enabled` (1/0), `since` and `until` (`YYYY-MM-DD HH:MM:SS`, UTC).

| endpoint | answers | extra params |
|---|---|---|
| `success_rate` | success rate and average steps per run, or per model/learning setting | `by_run` (1 = default, 0) |
| `steps_to_success` | average, median, p95, min and max steps of successful episodes | |
| `dead_action_rate` | share of steps that did not change the screen; share of clicks grounding couldn't place | |
| `action_hotspots` | actions that repeat or fail most (by type + target) | `min_uses` (2), `limit` (50) |
| `model_latency` | p50/p95/avg/max latency per call type and model | |
| `learning_curve` | success rate, avg steps, dead-action rate and lesson count per episode index | `bucket` (1) |
| `lesson_effectiveness` | success rate and steps with vs. without each lesson in the prompt | `min_episodes` (3) |

```sh
curl -H "Authorization: Bearer $READ_TOKEN" \
  "$TINYBIRD_HOST/v0/pipes/learning_curve.json?learning_enabled=1&bucket=5"
```

`lesson_effectiveness` shows correlation, not causation. Lessons pile up over time, so the episodes
without a lesson tend to be earlier ones. Compare within a window (`since`/`until`, `learning_enabled=1`)
and read each delta together with `episodes_with`.

`learning_curve` groups by `episode_index`, which restarts at 0 in every run. The index also follows the
seed (`--seed + index`). Without `run_id`, episode N of every run is pooled, even though `--learn` runs
start from whatever `lessons.json` already holds. To follow one learning run, filter by `run_id`, and
read `avg_lesson_count` next to each point.

## What leaves the machine, and what doesn't

- **Screenshots stay local.** Tinybird gets the frame's relative path (`runs/frames/seed3_07.png`), the
  sha256 of the file, and its width and height. `runs/artifacts.jsonl` records the same reference per
  run/episode/step. Frames are reused per seed across runs, so the hash tells you whether the file on disk
  is still the one a given step saw.
- **Prompts are redacted, then sent as a sha256 plus the first 2000 characters.** Redaction runs over
  every string in every event, and replaces:
  - the values of environment variables whose names contain TOKEN, SECRET, KEY, PASS, AUTH, CREDENTIAL,
    COOKIE or SESSION;
  - `Bearer`/`Basic` credentials and `token=`, `api_key:`, `password:` style values;
  - strings shaped like Tinybird, JWT, `sk-`, GitHub, Hugging Face, Slack or AWS keys.

  After redaction, strings are capped at 4000 characters.

  Redaction errs on the safe side. A phrase like "basic information" comes out as "basic [REDACTED]" in
  Tinybird. `trajectories.jsonl` keeps the original text, and lesson ids are computed from the original
  text too.
- No environment variables or headers are sent. The token only travels in the `Authorization` header.
  Redirects are refused: urllib would re-send the header to wherever a 3xx points. `TINYBIRD_HOST` must
  not contain credentials.
- Values that aren't secrets are sent as they are, after redaction. If you pass a local path as
  `--model`, or an exception message contains a path, that path (with your username) reaches Tinybird.
- Lesson ids are `sha256(normalized text)[:16]`, where normalized means NFKC, casefolded, with whitespace
  collapsed. The same lesson always gets the same id, and `lessons.json` keeps its format. "Active"
  lessons are the (up to 10) most recent ones the episode's prompt showed. `trajectories.jsonl` gains
  `episode_id` and `active_lesson_ids`.

## When Tinybird is slow or down

Events are buffered in memory. They are sent at the end of each episode, and once at run start so a bad
token or host shows up right away. Each request has a 3 s connect/read timeout. DNS lookup is not covered
by it, so a hanging resolver can stall a send for longer. Failures are handled like this:

- **Connection errors** never reached Tinybird. They are retried twice, with 0.5 s and 1 s backoff.
- **429 and 5xx** are retried the same way. These are Tinybird's retryable statuses. A 5xx from a proxy,
  after Tinybird had already accepted the batch, would duplicate it; every row carries an `event_id` to
  deduplicate on.
- **A request that got no answer is not retried**, because it may have landed and a resend would
  duplicate rows.
- **After 3 failed sends in a row**, telemetry turns itself off for the rest of the run.

Telemetry never raises into the agent. Episodes, local traces and reflection carry on either way.

## Tests

```sh
cd browser-use-loop
uv run python -m unittest discover -s tests
```

The tests mock HTTP and never call Tinybird. `tests/test_agent_telemetry.py` drives the real
`run_episode` against a fake MiniWoB env and a fake VLM.
