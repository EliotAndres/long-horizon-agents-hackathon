"""Optional Tinybird telemetry for the browser agent: traces -> Tinybird -> analytics (see tinybird/).

Off unless TINYBIRD_TELEMETRY_ENABLED=true and TINYBIRD_TOKEN are set (TINYBIRD_HOST defaults to
https://api.tinybird.co). Events are buffered in memory and posted to the Events API at the end of each
episode. A failed send is printed and dropped, so the agent, its local traces (runs/trajectories.jsonl,
runs/frames/, runs/lessons.json) and reflection carry on exactly as without telemetry.

Nothing sensitive leaves the machine: screenshots are referenced by relative path + sha256 + size, and
every string is redacted (secret-looking env values, bearer/API tokens, key=value secrets) before it is queued.
"""

import hashlib
import http.client
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

# event type -> Tinybird datasource (tinybird/datasources/<name>.datasource)
DATASOURCES = {
    "run_started": "agent_runs",
    "episode_started": "agent_episodes",
    "agent_step": "agent_steps",
    "model_call": "model_calls",
    "episode_result": "episode_results",
    "reflection_result": "reflection_events",
    "error": "agent_errors",
}
DEFAULT_HOST = "https://api.tinybird.co"
PROMPT_PREVIEW_CHARS = 2000
MAX_STR = 4000  # longer strings are truncated after redaction
REDACTED = "[REDACTED]"

SECRET_ENV_NAME = re.compile(r"TOKEN|SECRET|KEY|PASS|AUTH|CREDENTIAL|COOKIE|SESSION", re.IGNORECASE)
SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key|access[_-]?key|authorization)([\"']?\s*[:=]\s*[\"']?)"
                r"(?:(?:bearer|basic)\s+)?[^\s\"',;]{4,}"), r"\1\2" + REDACTED),
    (re.compile(r"\bp\.eyJ[\w.-]+"), REDACTED),  # Tinybird token
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), REDACTED),  # JWT
    (re.compile(r"\b(?:sk|pk|rk)-[\w-]{16,}|\bgh[pousr]_\w{20,}|\bhf_\w{20,}|\bxox[abpr]-[\w-]{10,}|\bAKIA[0-9A-Z]{16}\b"), REDACTED),
]


def secret_values(environ=os.environ):
    """Values of env vars whose names look secret (the Tinybird token among them), longest first."""
    return sorted({v for k, v in environ.items() if SECRET_ENV_NAME.search(k) and len(v) >= 8}, key=len, reverse=True)


def redact(text, secrets=()):
    for s in secrets:
        text = text.replace(s, REDACTED)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize(value, secrets=()):
    """Redact and cap every string in a value, turning anything that isn't JSON-native into a (redacted) string."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): sanitize(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, secrets) for v in value]
    value = redact(str(value), secrets)
    return value if len(value) <= MAX_STR else value[:MAX_STR] + "...[truncated]"


def sha256(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def lesson_id(text):
    """Stable id of a lesson: sha256 of its normalized text (NFKC, casefolded, whitespace collapsed), 16 hex chars."""
    return sha256(" ".join(unicodedata.normalize("NFKC", text).casefold().split()))[:16]


def screenshot_meta(path):
    """Reference to a local screenshot: relative path, sha256 of the file bytes, and size. The image stays local.
    Never raises: if the file can't be read, the hash is empty and the size 0."""
    path = Path(path)
    meta = {"screenshot_path": Path(os.path.relpath(path)).as_posix(), "screenshot_sha256": "",
            "screenshot_width": 0, "screenshot_height": 0}
    try:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as img:
            meta.update(screenshot_sha256=sha256(data), screenshot_width=img.width, screenshot_height=img.height)
    except OSError as e:  # PIL's UnidentifiedImageError is an OSError too
        _warn(f"could not read {meta['screenshot_path']}: {type(e).__name__}")
    return meta


def utc_now():
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class NoopTelemetry:
    """Used when Tinybird isn't configured: same interface as TinybirdTelemetry, records nothing."""

    def __init__(self):
        self.context = {}  # merged into every event (run_id, episode_id, step_index, ...)
        self.secrets = ()

    def emit(self, event_type, **fields):
        pass

    def model_call(self, call_type, prompt, output, latency_ms, **fields):
        """One VLM call (policy / grounding / reflection): the prompt goes out redacted, hashed and truncated."""
        clean = redact(prompt, self.secrets)
        self.emit("model_call", call_type=call_type, prompt_sha256=sha256(clean), prompt_chars=len(prompt),
                  prompt_preview=clean[:PROMPT_PREVIEW_CHARS], output_json=json.dumps(sanitize(output, self.secrets)),
                  latency_ms=round(latency_ms, 1), **fields)

    def error(self, component, exc):
        self.emit("error", component=component, error_type=type(exc).__name__, message=redact(str(exc), self.secrets)[:500], retry_count=0)

    def flush(self):
        pass

    def close(self):
        pass


class TinybirdTelemetry(NoopTelemetry):
    """Buffers events per datasource and posts them as NDJSON to Tinybird's Events API on flush().

    Never raises into the agent. Sends use Tinybird's default async ingestion (202 once accepted): waiting
    for the commit (wait=true) can stall for seconds under bursts. Timeouts are bounded; a request that never
    reached Tinybird (connection error, 429, 5xx) is retried with backoff, one that may have landed (no
    response in time) is not, so rows aren't duplicated. After `max_failures` failed sends in a row,
    telemetry turns itself off for the run.
    """

    def __init__(self, token, host=DEFAULT_HOST, secrets=(), timeout=3.0, max_retries=2, backoff=0.5,
                 max_failures=3, max_buffer=500):
        super().__init__()
        self._token = token
        self.host = host.rstrip("/")
        self.secrets = tuple(sorted({token, *secrets}, key=len, reverse=True))
        self.timeout, self.max_retries, self.backoff = timeout, max_retries, backoff
        self.max_failures, self.max_buffer = max_failures, max_buffer
        self.buffer = {}  # datasource -> [ndjson line]
        self.sent = self.dropped = self.failures = 0
        self.disabled = False

    def __repr__(self):  # keep the token out of tracebacks and debug prints
        return f"TinybirdTelemetry(host={self.host!r})"

    def emit(self, event_type, **fields):
        if self.disabled:
            self.dropped += 1
            return
        try:
            row = {"event_type": event_type, "event_id": uuid.uuid4().hex, "timestamp": utc_now(), **self.context, **fields}
            # serialize now, so later changes to the caller's lists (e.g. lessons) don't leak into the event
            self.buffer.setdefault(DATASOURCES[event_type], []).append(
                json.dumps(sanitize(row, self.secrets), ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 - telemetry must never break the agent
            _warn(f"could not record {event_type}: {type(e).__name__}")
            return
        if sum(map(len, self.buffer.values())) >= self.max_buffer:
            self.flush()

    def flush(self):
        try:
            for datasource in list(self.buffer):
                rows = self.buffer.pop(datasource, None)  # None if a nested flush already sent it
                if not rows:
                    continue
                if self.disabled:
                    self.dropped += len(rows)
                else:
                    self._send(datasource, rows)
        except Exception as e:  # noqa: BLE001 - telemetry must never break the agent
            _warn(f"flush failed: {type(e).__name__}")

    def close(self):
        self.flush()
        self.flush()  # error rows the first flush queued about its own failed sends
        self.dropped += sum(map(len, self.buffer.values()))
        self.buffer.clear()
        _warn(f"sent {self.sent} events to Tinybird, gave up on {self.dropped}")

    def _send(self, datasource, rows):
        req = urllib.request.Request(
            f"{self.host}/v0/events?" + urllib.parse.urlencode({"name": datasource}),
            data=("\n".join(rows) + "\n").encode(), method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/x-ndjson"})
        attempt = 0
        while True:
            try:
                with _opener.open(req, timeout=self.timeout) as resp:
                    body = resp.read()
                self.sent += len(rows)
                self.failures = 0
                _check_quarantine(datasource, body)
                return
            except urllib.error.HTTPError as e:  # Tinybird answered: only rate limits and server errors are worth a retry
                error_type, message, retry = "HTTPError", f"HTTP {e.code} {e.reason}", e.code == 429 or e.code >= 500
            except urllib.error.URLError as e:  # the request never reached Tinybird: safe to resend
                error_type, message, retry = "URLError", f"connection failed: {e.reason}", True
            except (OSError, http.client.HTTPException) as e:  # sent but no (full) answer: it may have landed, don't duplicate it
                error_type, message, retry = type(e).__name__, f"no complete response (timeout {self.timeout}s)", False
            if not retry or attempt >= self.max_retries:
                break
            time.sleep(self.backoff * 2 ** attempt)
            attempt += 1
        self.failures += 1
        self.dropped += len(rows)
        message = redact(f"send to {datasource} failed: {message}", self.secrets)
        _warn(f"{message}; gave up on {len(rows)} events (local traces are unaffected)")
        self.emit("error", component="telemetry", error_type=error_type, message=message, retry_count=attempt)
        if self.failures >= self.max_failures:
            self.disabled = True
            _warn(f"{self.failures} failed sends in a row, telemetry is off for the rest of this run")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends every header, Authorization included, to wherever a redirect points (any host, even
    plain http). Refuse: a 3xx becomes a failed send (HTTPError)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _check_quarantine(datasource, body):
    try:
        quarantined = json.loads(body or b"{}").get("quarantined_rows", 0)
    except (ValueError, TypeError, AttributeError):  # not the JSON we expect: nothing to report
        return
    if quarantined:
        _warn(f"{quarantined} rows quarantined in {datasource}: schema mismatch, see tinybird/datasources/{datasource}.datasource")


def _warn(message):
    print(f"[telemetry] {message}", file=sys.stderr)


def from_env(environ=os.environ):
    """TinybirdTelemetry if enabled and configured, else NoopTelemetry (with a warning if it looks misconfigured)."""
    if environ.get("TINYBIRD_TELEMETRY_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        return NoopTelemetry()
    token = environ.get("TINYBIRD_TOKEN", "").strip()
    host = (environ.get("TINYBIRD_HOST") or DEFAULT_HOST).strip().rstrip("/")
    url = urllib.parse.urlsplit(host)
    if not token:
        _warn("TINYBIRD_TELEMETRY_ENABLED is set but TINYBIRD_TOKEN is empty; telemetry is off")
        return NoopTelemetry()
    # the token travels in a header: never send it in clear text, except to Tinybird Local
    if url.username or url.password or not url.hostname or not (
            url.scheme == "https" or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1"))):
        _warn("TINYBIRD_HOST must be an https:// URL without credentials (http:// only for localhost); telemetry is off")
        return NoopTelemetry()
    _warn(f"sending events to {url.scheme}://{url.netloc}")  # netloc has no credentials: rejected above
    return TinybirdTelemetry(token, host, secrets=secret_values(environ))
