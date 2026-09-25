"""Unit tests for telemetry.py. No network beyond a localhost stub: the HTTP opener is mocked.

    uv run python -m unittest discover -s tests
"""

import contextlib
import email.message
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from PIL import Image

import telemetry
from telemetry import NoopTelemetry, TinybirdTelemetry

TOKEN = "p.eyJ1IjogIjEyMzQ1Njc4LWFiY2QtNGVmMC05ODc2LTEyMzQ1Njc4OWFiYyIsICJpZCI6ICJ4In0.c2lnbmF0dXJlLXZhbHVl"


def ok_response(successful=1, quarantined=0):
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(
        {"successful_rows": successful, "quarantined_rows": quarantined}).encode()
    return resp


def http_error(code):
    return urllib.error.HTTPError("https://api.tinybird.co/v0/events", code, "err", email.message.Message(), None)


def make(**kw):
    return TinybirdTelemetry(TOKEN, "https://api.tinybird.co", backoff=0, **kw)


def rows(tel, datasource):
    return [json.loads(line) for line in tel.buffer.get(datasource, [])]


def sent_bodies(urlopen):
    """NDJSON rows of every request the mocked urlopen received, as (request, [row])."""
    out = []
    for call in urlopen.call_args_list:
        req = call.args[0]
        out.append((req, [json.loads(line) for line in req.data.decode().splitlines()]))
    return out


class SerializationTest(unittest.TestCase):
    def test_every_event_type_goes_to_its_datasource(self):
        tel = make()
        for event_type, datasource in telemetry.DATASOURCES.items():
            tel.emit(event_type, x=1)
            self.assertEqual(rows(tel, datasource)[-1]["event_type"], event_type)
        self.assertEqual(sum(map(len, tel.buffer.values())), len(telemetry.DATASOURCES))

    def test_row_has_envelope_context_and_fields(self):
        tel = make()
        tel.context.update(run_id="20260925-120000", episode_id="20260925-120000-003", step_index=4)
        tel.emit("agent_step", action_type="click", screen_changed=False, step_latency_ms=12.5, active=["a"])
        (row,) = rows(tel, "agent_steps")
        self.assertRegex(row["event_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(row["timestamp"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}$")  # DateTime64(3), UTC
        self.assertEqual((row["run_id"], row["episode_id"], row["step_index"]), ("20260925-120000", "20260925-120000-003", 4))
        self.assertEqual((row["action_type"], row["screen_changed"], row["step_latency_ms"]), ("click", False, 12.5))
        self.assertEqual(row["active"], ["a"])

    def test_fields_override_context(self):
        tel = make()
        tel.context["step_index"] = 3
        tel.emit("model_call", step_index=None)
        self.assertIsNone(rows(tel, "model_calls")[0]["step_index"])

    def test_payload_is_frozen_at_emit_time(self):
        tel = make()
        lessons = ["first lesson"]
        tel.emit("episode_started", active_lessons=lessons)
        lessons.append("added later by reflection")
        self.assertEqual(rows(tel, "agent_episodes")[0]["active_lessons"], ["first lesson"])

    def test_model_call_hashes_and_truncates_prompt(self):
        tel = make()
        prompt = "Task: book a flight\n" + "x" * 5000
        tel.model_call("policy", prompt, {"state": "s", "action": "click", "arg": "Search"}, 1234.567, image_count=2)
        (row,) = rows(tel, "model_calls")
        self.assertEqual(row["call_type"], "policy")
        self.assertEqual(row["prompt_sha256"], hashlib.sha256(prompt.encode()).hexdigest())
        self.assertEqual(row["prompt_chars"], len(prompt))
        self.assertEqual(len(row["prompt_preview"]), telemetry.PROMPT_PREVIEW_CHARS)
        self.assertEqual(json.loads(row["output_json"]), {"state": "s", "action": "click", "arg": "Search"})
        self.assertEqual((row["latency_ms"], row["image_count"]), (1234.6, 2))

    def test_error_event(self):
        tel = make()
        tel.error("grounding", ValueError("bad bbox"))
        (row,) = rows(tel, "agent_errors")
        self.assertEqual((row["component"], row["error_type"], row["message"], row["retry_count"]),
                         ("grounding", "ValueError", "bad bbox", 0))

    def test_long_strings_are_capped(self):
        tel = make()
        tel.emit("agent_step", model_state="y" * 10_000)
        self.assertLess(len(rows(tel, "agent_steps")[0]["model_state"]), telemetry.MAX_STR + 20)

    def test_emit_never_raises(self):
        tel = make()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            tel.emit("not_an_event_type", x=1)
            tel.emit("agent_step", odd=object())  # not JSON-able: stringified, not fatal
        self.assertIn("could not record not_an_event_type", err.getvalue())
        self.assertEqual(len(rows(tel, "agent_steps")), 1)


class RedactionTest(unittest.TestCase):
    def test_patterns(self):
        cases = {
            "Authorization: Bearer abcdefgh12345678xyz": "abcdefgh12345678xyz",
            "use token=abc123secret please": "abc123secret",
            'config {"api_key": "sk-proj-1234567890abcdefghij"}': "sk-proj-1234567890abcdefghij",
            "password: hunter22": "hunter22",
            f"tinybird {TOKEN} here": TOKEN,
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U": "eyJhbGciOiJIUzI1NiJ9",
            "gh ghp_abcdefghijklmnopqrstuvwxyz0123": "ghp_abcdefghijklmnopqrstuvwxyz0123",
            "hf hf_abcdefghijklmnopqrstuvwxyz": "hf_abcdefghijklmnopqrstuvwxyz",
            "aws AKIAABCDEFGHIJKLMNOP": "AKIAABCDEFGHIJKLMNOP",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                out = telemetry.redact(text)
                self.assertNotIn(secret, out)
                self.assertIn(telemetry.REDACTED, out)

    def test_ordinary_text_is_left_alone(self):
        for text in ('Book the shortest one-way flight from: Tampa, FL to: KCK on 10/15/2016. click("the password field")',
                     "the token field is empty", "type the date into the calendar"):
            with self.subTest(text=text):
                self.assertEqual(telemetry.redact(text), text)

    def test_bearer_and_basic_err_on_the_side_of_redacting(self):
        # a token need not contain a digit, so "basic <long word>" is redacted too: fidelity loses to safety here
        self.assertEqual(telemetry.redact("Bearer abcdefghijklmnopqrstu"), "Bearer [REDACTED]")
        self.assertEqual(telemetry.redact("the basic information form"), "the basic [REDACTED] form")

    def test_auth_header_and_json_forms(self):
        self.assertEqual(telemetry.redact("Authorization: Basic dXNlcjpwYXNz"), "Authorization: [REDACTED]")
        self.assertEqual(telemetry.redact('{"password": "hunter2xx", "user": "a"}'), '{"password": "[REDACTED]", "user": "a"}')
        self.assertEqual(telemetry.redact("Bearer abcdefgh12345678"), "Bearer [REDACTED]")

    def test_secret_env_values(self):
        env = {"HF_TOKEN": "hf-local-value-123", "MY_API_KEY": "zzzzzzzzzz", "DB_PASSWORD": "correcthorse",
               "HOME": "/Users/someone", "SHORT_TOKEN": "abc"}
        secrets = telemetry.secret_values(env)
        self.assertEqual(set(secrets), {"hf-local-value-123", "zzzzzzzzzz", "correcthorse"})
        self.assertEqual(telemetry.redact("x correcthorse y /Users/someone", secrets), "x [REDACTED] y /Users/someone")

    def test_sanitize_is_recursive(self):
        out = telemetry.sanitize({"a": ["token: abcdef123", {"b": "Bearer abcdefgh12345678"}], "n": 3, "ok": True})
        self.assertEqual(out, {"a": ["token: [REDACTED]", {"b": "Bearer [REDACTED]"}], "n": 3, "ok": True})

    def test_token_and_env_secrets_never_reach_a_row(self):
        tel = TinybirdTelemetry(TOKEN, secrets=["my-secret-env-value"])
        tel.context["run_id"] = "r"
        tel.emit("agent_step", model_state=f"I see {TOKEN}", action_arg="my-secret-env-value", notes=["x my-secret-env-value"])
        tel.model_call("policy", f"prompt with {TOKEN} and my-secret-env-value", {"arg": TOKEN}, 1.0)
        tel.error("policy", RuntimeError(f"auth failed for {TOKEN}"))
        payload = "\n".join(line for lines in tel.buffer.values() for line in lines)
        self.assertNotIn(TOKEN, payload)
        self.assertNotIn(TOKEN[:20], payload)
        self.assertNotIn("my-secret-env-value", payload)
        self.assertEqual(payload.count(telemetry.REDACTED), 7)

    def test_prompt_hash_is_of_the_redacted_prompt(self):
        tel = TinybirdTelemetry(TOKEN)
        tel.model_call("policy", f"secret {TOKEN}", {}, 1.0)
        (row,) = rows(tel, "model_calls")
        self.assertEqual(row["prompt_sha256"], hashlib.sha256(b"secret [REDACTED]").hexdigest())

    def test_non_string_values_are_redacted_too(self):
        class Leaky:
            def __str__(self):
                return "token=abcdef123456"

        self.assertEqual(telemetry.sanitize({"x": Leaky(), 1: [None, True, 2.5]}), {"x": "token=[REDACTED]", "1": [None, True, 2.5]})

    def test_error_message_is_redacted_before_it_is_cut(self):
        tel = TinybirdTelemetry(TOKEN, secrets=["my-secret-env-value"])
        tel.error("run", RuntimeError("x" * 490 + "my-secret-env-value"))
        self.assertNotIn("my-secret", rows(tel, "agent_errors")[0]["message"])

    def test_structured_output_is_redacted_before_json_escaping(self):
        tel = TinybirdTelemetry(TOKEN, secrets=["pässwört-geheim-1"])
        tel.model_call("policy", "p", {"arg": 'pässwört-geheim-1 "quoted"'}, 1.0)
        self.assertEqual(json.loads(rows(tel, "model_calls")[0]["output_json"]), {"arg": '[REDACTED] "quoted"'})

    def test_token_not_in_repr(self):
        self.assertNotIn(TOKEN, repr(make()))


@mock.patch("telemetry.time.sleep")
@mock.patch.object(telemetry._opener, "open")
class SendTest(unittest.TestCase):
    def test_successful_send(self, urlopen, sleep):
        urlopen.return_value = ok_response(2)
        tel = make()
        tel.context["run_id"] = "r1"
        tel.emit("agent_step", step_index=0)
        tel.emit("agent_step", step_index=1)
        tel.emit("episode_result", success=True)
        with contextlib.redirect_stderr(io.StringIO()):
            tel.flush()
        sent = sent_bodies(urlopen)
        self.assertEqual(len(sent), 2)  # one request per datasource
        by_name = {urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)["name"][0]: (req, body) for req, body in sent}
        req, body = by_name["agent_steps"]
        self.assertEqual(req.full_url, "https://api.tinybird.co/v0/events?name=agent_steps")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, req.full_url)
        self.assertEqual([r["step_index"] for r in body], [0, 1])
        self.assertTrue(all(r["run_id"] == "r1" for r in body))
        self.assertEqual(by_name["episode_results"][1][0]["success"], True)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], tel.timeout)
        self.assertEqual((tel.sent, tel.dropped, tel.buffer), (3, 0, {}))
        sleep.assert_not_called()

    def test_flush_with_nothing_buffered_sends_nothing(self, urlopen, sleep):
        make().flush()
        urlopen.assert_not_called()

    def test_quarantined_rows_are_reported(self, urlopen, sleep):
        urlopen.return_value = ok_response(0, quarantined=1)
        tel = make()
        tel.emit("agent_step")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            tel.flush()
        self.assertIn("1 rows quarantined in agent_steps", err.getvalue())

    def test_tinybird_unreachable_is_retried_then_dropped(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        tel = make(max_retries=2)
        tel.emit("agent_step")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            tel.flush()  # must not raise
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual((tel.sent, tel.dropped, tel.failures), (0, 1, 1))
        self.assertIn("send to agent_steps failed", err.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        (error_row,) = rows(tel, "agent_errors")  # queued for the next successful send
        self.assertEqual((error_row["component"], error_row["error_type"], error_row["retry_count"]), ("telemetry", "URLError", 2))

    def test_timeout_is_not_retried(self, urlopen, sleep):
        """The request went out but no answer came: it may have landed, so resending could duplicate rows."""
        urlopen.side_effect = TimeoutError("The read operation timed out")
        tel = make(timeout=0.5)
        tel.emit("agent_step")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            tel.flush()
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 0.5)
        self.assertEqual((tel.dropped, tel.failures), (1, 1))
        self.assertIn("no complete response (timeout 0.5s)", err.getvalue())
        self.assertEqual(rows(tel, "agent_errors")[0]["error_type"], "TimeoutError")

    def test_socket_timeout_while_connecting_is_retried(self, urlopen, sleep):
        urlopen.side_effect = [urllib.error.URLError(TimeoutError("timed out")), ok_response()]
        tel = make()
        tel.emit("agent_step")
        tel.flush()
        self.assertEqual((urlopen.call_count, tel.sent, tel.dropped), (2, 1, 0))

    def test_http_status_retry_policy(self, urlopen, sleep):
        for code, calls in ((401, 1), (403, 1), (404, 1), (400, 1), (429, 3), (500, 3), (503, 3)):
            with self.subTest(code=code):
                urlopen.reset_mock()
                urlopen.side_effect = http_error(code)
                tel = make(max_retries=2)
                tel.emit("agent_step")
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    tel.flush()
                self.assertEqual(urlopen.call_count, calls)
                self.assertIn(f"HTTP {code}", err.getvalue())

    def test_turns_itself_off_after_repeated_failures(self, urlopen, sleep):
        urlopen.side_effect = http_error(401)
        tel = make(max_failures=3)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for _ in range(5):
                tel.emit("agent_step")
                tel.flush()
            tel.emit("agent_step")
            tel.close()
        self.assertEqual(urlopen.call_count, 3)
        self.assertTrue(tel.disabled)
        self.assertEqual(tel.buffer, {})
        self.assertIn("telemetry is off for the rest of this run", err.getvalue())

    def test_success_resets_the_failure_count(self, urlopen, sleep):
        fail = http_error(401)
        urlopen.side_effect = [fail, fail, ok_response(), fail, fail, ok_response()]
        tel = make(max_failures=3)
        with contextlib.redirect_stderr(io.StringIO()):
            for _ in range(6):
                tel.emit("agent_step")
                tel.flush()
        self.assertFalse(tel.disabled)

    def test_unexpected_exception_does_not_escape(self, urlopen, sleep):
        urlopen.side_effect = RuntimeError("boom")
        tel = make()
        tel.emit("agent_step")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            tel.flush()
            tel.close()
        self.assertIn("flush failed: RuntimeError", err.getvalue())

    def test_one_failing_datasource_does_not_stop_the_others(self, urlopen, sleep):
        urlopen.side_effect = [http_error(404), ok_response()]
        tel = make()
        tel.emit("agent_step")
        tel.emit("episode_result")
        with contextlib.redirect_stderr(io.StringIO()):
            tel.flush()
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual((tel.sent, tel.dropped), (1, 1))

    def test_close_sends_the_error_rows_its_flush_queued(self, urlopen, sleep):
        urlopen.side_effect = [urllib.error.URLError("down")] * 3 + [ok_response()]
        tel = make(max_retries=2)
        tel.emit("agent_step")
        with contextlib.redirect_stderr(io.StringIO()):
            tel.close()
        self.assertEqual(tel.buffer, {})
        req, body = sent_bodies(urlopen)[-1]
        self.assertIn("name=agent_errors", req.full_url)
        self.assertEqual(body[0]["component"], "telemetry")

    def test_buffer_flushes_when_full(self, urlopen, sleep):
        urlopen.return_value = ok_response()
        tel = make(max_buffer=3)
        for _ in range(3):
            tel.emit("agent_step")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(tel.buffer, {})


class RedirectTest(unittest.TestCase):
    def test_redirect_is_refused_and_the_token_does_not_follow_it(self):
        followed = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # keep the test output quiet
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{server.server_port}/elsewhere")
                self.end_headers()

            def do_GET(self):
                followed.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tel = TinybirdTelemetry(TOKEN, f"http://127.0.0.1:{server.server_port}", backoff=0)
        tel.emit("agent_step")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            tel.flush()
        self.assertEqual(followed, [])
        self.assertEqual((tel.sent, tel.dropped), (0, 1))
        self.assertIn("HTTP 302", err.getvalue())


class NoopTest(unittest.TestCase):
    @mock.patch.object(telemetry._opener, "open")
    def test_noop_records_and_sends_nothing(self, urlopen):
        tel = NoopTelemetry()
        tel.context["run_id"] = "r"
        tel.emit("agent_step", x=1)
        tel.model_call("policy", "prompt", {"a": 1}, 3.0)
        tel.error("run", KeyboardInterrupt())
        tel.flush()
        tel.close()
        urlopen.assert_not_called()


class FromEnvTest(unittest.TestCase):
    def setUp(self):
        self.stderr = contextlib.redirect_stderr(io.StringIO())
        self.err = self.stderr.__enter__()

    def tearDown(self):
        self.stderr.__exit__(None, None, None)

    def test_rejects_credentials_in_the_host_and_never_prints_them(self):
        tel = telemetry.from_env({"TINYBIRD_TELEMETRY_ENABLED": "true", "TINYBIRD_TOKEN": TOKEN,
                                  "TINYBIRD_HOST": "https://user:hunter2@api.tinybird.co"})
        self.assertIs(type(tel), NoopTelemetry)
        self.assertNotIn("hunter2", self.err.getvalue())

    def test_off_by_default(self):
        self.assertIs(type(telemetry.from_env({"TINYBIRD_TOKEN": TOKEN})), NoopTelemetry)
        self.assertIs(type(telemetry.from_env({"TINYBIRD_TELEMETRY_ENABLED": "false", "TINYBIRD_TOKEN": TOKEN})), NoopTelemetry)

    def test_enabled_without_token(self):
        self.assertIs(type(telemetry.from_env({"TINYBIRD_TELEMETRY_ENABLED": "true"})), NoopTelemetry)
        self.assertIn("TINYBIRD_TOKEN is empty", self.err.getvalue())

    def enabled(self, **env):
        tel = telemetry.from_env({"TINYBIRD_TELEMETRY_ENABLED": "true", "TINYBIRD_TOKEN": TOKEN, **env})
        assert isinstance(tel, TinybirdTelemetry)
        return tel

    def test_enabled(self):
        tel = self.enabled(HF_TOKEN="hf-local-value-123")
        self.assertEqual(tel.host, telemetry.DEFAULT_HOST)
        self.assertIn("hf-local-value-123", tel.secrets)
        self.assertNotIn(TOKEN, self.err.getvalue())

    def test_custom_host(self):
        self.assertEqual(self.enabled(TINYBIRD_HOST="https://api.us-east.aws.tinybird.co/").host, "https://api.us-east.aws.tinybird.co")
        self.assertEqual(self.enabled(TINYBIRD_HOST="http://localhost:7181").host, "http://localhost:7181")
        self.assertIsInstance(telemetry.from_env({"TINYBIRD_TELEMETRY_ENABLED": "1", "TINYBIRD_TOKEN": TOKEN}), TinybirdTelemetry)

    def test_refuses_to_send_the_token_in_clear_text(self):
        env = {"TINYBIRD_TELEMETRY_ENABLED": "true", "TINYBIRD_TOKEN": TOKEN}
        for host in ("http://api.tinybird.co", "ftp://x", "api.tinybird.co", "https://", "http://localhost.evil.com",
                     "http://127.0.0.1@evil.com"):
            with self.subTest(host=host):
                self.assertIs(type(telemetry.from_env({**env, "TINYBIRD_HOST": host})), NoopTelemetry)


class LessonIdTest(unittest.TestCase):
    LESSON = "If a calendar is open, pick the date on it instead of typing."

    def test_definition(self):
        normalized = "if a calendar is open, pick the date on it instead of typing."
        self.assertEqual(telemetry.lesson_id(self.LESSON), hashlib.sha256(normalized.encode()).hexdigest()[:16])

    def test_pinned_value(self):
        # ids are stored in Tinybird: changing the normalization would orphan every past row
        self.assertEqual(telemetry.lesson_id(self.LESSON), "6611eed139aaa196")

    def test_stable_across_formatting(self):
        variants = ["  if a CALENDAR is open,  pick the date\non it instead of typing.  ",
                    "If a calendar is open, pick the date on it instead of typing.",  # full-width comma (NFKC)
                    "If\ta calendar is open, pick the date on it instead of typing."]
        for v in variants:
            with self.subTest(v=v):
                self.assertEqual(telemetry.lesson_id(v), telemetry.lesson_id(self.LESSON))

    def test_different_lessons_differ(self):
        self.assertNotEqual(telemetry.lesson_id(self.LESSON), telemetry.lesson_id("If a calendar is open, type the date."))
        self.assertRegex(telemetry.lesson_id("x"), r"^[0-9a-f]{16}$")


class ScreenshotMetaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("runs/frames").mkdir(parents=True)
        Image.new("RGB", (160, 210), "white").save("runs/frames/seed3_07.png")
        self.data = Path("runs/frames/seed3_07.png").read_bytes()

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_metadata(self):
        meta = telemetry.screenshot_meta(Path("runs/frames/seed3_07.png"))
        self.assertEqual(meta, {"screenshot_path": "runs/frames/seed3_07.png",
                                "screenshot_sha256": hashlib.sha256(self.data).hexdigest(),
                                "screenshot_width": 160, "screenshot_height": 210})

    def test_absolute_path_is_made_relative(self):
        meta = telemetry.screenshot_meta(Path.cwd() / "runs/frames/seed3_07.png")
        self.assertEqual(meta["screenshot_path"], "runs/frames/seed3_07.png")

    def test_unreadable_screenshot_does_not_raise(self):
        Path("runs/frames/broken.png").write_bytes(b"not a png")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for name in ("broken.png", "missing.png"):
                meta = telemetry.screenshot_meta(f"runs/frames/{name}")
                self.assertEqual(meta, {"screenshot_path": f"runs/frames/{name}", "screenshot_sha256": "",
                                        "screenshot_width": 0, "screenshot_height": 0})
        self.assertIn("could not read runs/frames/missing.png", err.getvalue())

    def test_no_image_bytes_in_the_event(self):
        tel = make()
        tel.emit("agent_step", **telemetry.screenshot_meta("runs/frames/seed3_07.png"))
        (row,) = rows(tel, "agent_steps")
        self.assertEqual({k for k in row if k.startswith("screenshot")},
                         {"screenshot_path", "screenshot_sha256", "screenshot_width", "screenshot_height"})
        self.assertLess(len(tel.buffer["agent_steps"][0]), 400)


if __name__ == "__main__":
    unittest.main()
