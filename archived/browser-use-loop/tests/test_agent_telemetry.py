"""Runs agent.run_episode against a fake MiniWoB env and a fake VLM, to check the telemetry hooks end to end
and that the agent's own outputs (trajectories, frames, lessons) don't depend on Tinybird. No network, no model.

    uv run python -m unittest discover -s tests
"""

import contextlib
import hashlib
import io
import itertools
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

import telemetry

try:
    from miniwob.action import ActionTypes

    import agent
except (ImportError, OSError) as e:  # agent.py needs mlx-vlm, miniwob and a macOS font
    raise unittest.SkipTest(f"agent.py not importable here: {e}") from e

TASK = "Book the shortest one-way flight from: Tampa, FL to: KCK on 10/15/2016."
LESSONS = [f"If situation {i} happens, do thing {i}." for i in range(12)]
NEW_LESSON = "If typing did not change the page, click the input field first."
# policy decisions, one per step: click (screen changes), type (screen does not change),
# click on something grounding can't find (not executed), click that ends the episode
POLICY = [("click", "the From input"), ("type", "Tampa"), ("click", "Nonexistent thing"), ("click", "Book flight")]
ENV_SCRIPT = [(1, False, 0.0), (1, False, 0.0), (2, True, 1.0)]  # (screen, done, raw_reward) per executed action


def screen(n):
    img = np.zeros((agent.H, agent.W, 3), np.uint8)
    img[: n * 10 + 1] = 255
    return img


class FakeEnv:
    def __init__(self):
        driver = SimpleNamespace(execute_script=self.script)
        self.unwrapped = SimpleNamespace(instance=SimpleNamespace(driver=driver), create_action=lambda kind, **kw: (kind, kw))
        self.i = 0
        self.closed = False

    def close(self):
        self.closed = True

    @staticmethod
    def script(js, *args):
        if "elementFromPoint" in js:
            return 'button#search "Search"'
        if "activeElement; return" in js:
            return "input#flight-from"
        return None

    def reset(self, seed=None):
        self.i = 0
        return {}, {}

    def step(self, action):
        if action[0] == ActionTypes.NONE:
            return {"utterance": TASK, "screenshot": screen(0)}, 0, False, False, {}
        n, done, reward = ENV_SCRIPT[self.i]
        self.i += 1
        return {"utterance": TASK, "screenshot": screen(n)}, 0, done, False, {"raw_reward": reward}


class FakeVLM:
    """Stands in for mlx_vlm.generate; answers by prompt kind and records the prompts it saw."""

    def __init__(self, interrupt_at=None):
        self.policy = itertools.cycle(POLICY)  # the same four steps every episode
        self.prompts = []
        self.interrupt_at = interrupt_at  # raise KeyboardInterrupt on this policy call (1-based), like Ctrl-C
        self.policy_calls = 0

    def generate(self, model, processor, prompt, **kw):
        self.prompts.append(prompt)
        if prompt.startswith("Detect"):
            out = {"bbox": [5, 5, 5, 5] if "Nonexistent" in prompt else [100, 100, 300, 200]}
        elif prompt.startswith("You are reviewing"):
            out = {"remove": [0], "lessons": [NEW_LESSON]}
        else:
            self.policy_calls += 1
            if self.policy_calls == self.interrupt_at:
                raise KeyboardInterrupt
            action, arg = next(self.policy)
            out = {"state": "the form is visible", "action": action, "arg": arg}
        return SimpleNamespace(text=json.dumps(out), prompt_tokens=321, generation_tokens=12, finish_reason="stop")


class RunEpisodeTelemetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        agent.FRAMES_DIR.mkdir(parents=True)
        self.vlm = FakeVLM()
        for name, fake in (("generate", self.vlm.generate), ("apply_chat_template", lambda proc, cfg, prompt, **kw: prompt),
                           ("build_json_schema_logits_processor", lambda tok, schema: None)):
            patcher = mock.patch.object(agent, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def tearDown(self):
        agent.TELEMETRY = telemetry.NoopTelemetry()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def run_episode(self, urlopen_effect):
        tel = telemetry.TinybirdTelemetry("p.test-token", "https://tinybird.test", backoff=0)
        tel.context.update(run_id=agent.RUN_ID, model="fake-model", learning_enabled=True)
        agent.TELEMETRY = tel
        lessons, log = list(LESSONS), io.StringIO()
        with mock.patch.object(telemetry._opener, "open", **urlopen_effect) as urlopen, contextlib.redirect_stdout(io.StringIO()):
            reward = agent.run_episode(FakeEnv(), SimpleNamespace(config=None), SimpleNamespace(tokenizer=None), 5, 10, log,
                                       lessons=lessons, episode_index=0)
        return tel, urlopen, reward, lessons, json.loads(log.getvalue())

    @staticmethod
    def events(urlopen):
        by_ds = {}
        for call in urlopen.call_args_list:
            req = call.args[0]
            name = req.full_url.split("name=")[1]
            by_ds.setdefault(name, []).extend(json.loads(line) for line in req.data.decode().splitlines())
        return by_ds

    def assert_local_outputs(self, reward, lessons, record):
        """What the agent writes locally, with or without Tinybird."""
        self.assertEqual(reward, 1.0)
        self.assertEqual(set(record), {"run", "seed", "task", "reward", "steps", "lessons_added", "lessons_removed",
                                       "episode_id", "active_lesson_ids"})
        self.assertEqual([s["changed"] for s in record["steps"]], [True, False, True, True])
        self.assertEqual(record["steps"][2]["result"], 'click("Nonexistent thing") -> FAILED, could not find it on screen')
        self.assertEqual(record["active_lesson_ids"], [telemetry.lesson_id(l) for l in LESSONS[-10:]])
        self.assertEqual((record["lessons_added"], record["lessons_removed"]), ([NEW_LESSON], [LESSONS[0]]))
        self.assertEqual(lessons, LESSONS[1:] + [NEW_LESSON])  # reflection still ran
        self.assertEqual(json.loads(agent.LESSONS_FILE.read_text()), lessons)
        self.assertEqual(sorted(p.name for p in agent.FRAMES_DIR.iterdir()), [f"seed5_{t:02d}.png" for t in range(4)])
        artifacts = [json.loads(l) for l in agent.ARTIFACTS_FILE.read_text().splitlines()]
        self.assertEqual([a["step"] for a in artifacts], [0, 1, 2, 3])
        self.assertEqual(artifacts[0]["screenshot_sha256"], hashlib.sha256(Path("runs/frames/seed5_00.png").read_bytes()).hexdigest())

    def test_events_sent_for_an_episode(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"successful_rows": 1, "quarantined_rows": 0}'
        tel, urlopen, reward, lessons, record = self.run_episode({"return_value": ok})
        self.assert_local_outputs(reward, lessons, record)
        self.assertEqual((tel.sent, tel.dropped), (1 + 4 + 8 + 1 + 1, 0))
        ev = self.events(urlopen)
        episode_id = f"{agent.RUN_ID}-000"

        (started,) = ev["agent_episodes"]
        self.assertEqual((started["episode_id"], started["seed"], started["task"], started["known_lesson_count"]), (episode_id, 5, TASK, 12))
        self.assertEqual(started["active_lessons"], LESSONS[-10:])  # the lessons the prompt shows
        self.assertEqual(started["active_lesson_ids"], [telemetry.lesson_id(l) for l in LESSONS[-10:]])
        policy_prompt = self.vlm.prompts[0]
        self.assertIn(f"Lesson from past attempts: {LESSONS[2]}", policy_prompt)
        self.assertNotIn(f"Lesson from past attempts: {LESSONS[1]}\n", policy_prompt)

        steps = ev["agent_steps"]
        self.assertEqual([s["step_index"] for s in steps], [0, 1, 2, 3])
        self.assertEqual([s["action_type"] for s in steps], ["click", "type", "click", "click"])
        self.assertEqual([s["executed"] for s in steps], [True, True, False, True])
        self.assertEqual([s["screen_changed"] for s in steps], [True, False, True, True])
        self.assertEqual([s["done"] for s in steps], [False, False, False, True])
        self.assertEqual([s["action_result"] for s in steps], [s["result"] for s in record["steps"]])
        self.assertTrue(all(s["episode_id"] == episode_id and s["run_id"] == agent.RUN_ID and s["lesson_count"] == 12 for s in steps))
        self.assertEqual(steps[1]["screenshot_path"], "runs/frames/seed5_01.png")
        self.assertEqual(steps[1]["screenshot_sha256"], hashlib.sha256(Path("runs/frames/seed5_01.png").read_bytes()).hexdigest())
        self.assertEqual((steps[1]["screenshot_width"], steps[1]["screenshot_height"]), (agent.W, agent.H))
        self.assertGreater(steps[0]["step_latency_ms"], 0)

        calls = ev["model_calls"]
        self.assertEqual([c["call_type"] for c in calls].count("policy"), 4)
        self.assertEqual([c["call_type"] for c in calls].count("grounding"), 3)
        (reflection,) = [c for c in calls if c["call_type"] == "reflection"]
        self.assertIsNone(reflection["step_index"])
        grounding = [c for c in calls if c["call_type"] == "grounding"]
        self.assertEqual([c["step_index"] for c in grounding], [0, 2, 3])
        self.assertEqual(json.loads(grounding[1]["output_json"]), {"bbox": [5, 5, 5, 5]})
        self.assertEqual((calls[0]["prompt_tokens"], calls[0]["generation_tokens"], calls[0]["finish_reason"]), (321, 12, "stop"))

        (result,) = ev["episode_results"]
        self.assertEqual((result["reward"], result["success"], result["steps"], result["no_change_steps"], result["lesson_count"]),
                         (1.0, True, 4, 1, 12))
        self.assertEqual(result["active_lesson_ids"], started["active_lesson_ids"])
        (refl,) = ev["reflection_events"]
        self.assertEqual((refl["lessons_before"], refl["lessons_after_count"]), (12, 12))
        self.assertEqual((refl["lessons_added"], refl["lessons_removed"]), ([NEW_LESSON], [LESSONS[0]]))
        self.assertEqual(refl["lessons_added_ids"], [telemetry.lesson_id(NEW_LESSON)])
        self.assertNotIn("agent_errors", ev)

    def test_episode_and_local_traces_survive_tinybird_being_down(self):
        tel, urlopen, reward, lessons, record = self.run_episode(
            {"side_effect": urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))})
        self.assert_local_outputs(reward, lessons, record)
        self.assertGreater(urlopen.call_count, 0)
        self.assertEqual(tel.sent, 0)
        self.assertTrue(tel.disabled)
        self.assertIn("telemetry is off for the rest of this run", self.stderr.getvalue())

    def test_artifacts_index_failure_does_not_stop_the_episode(self):
        agent.ARTIFACTS_FILE.mkdir()  # appending to a directory fails with IsADirectoryError
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"{}"
        _, urlopen, reward, _, record = self.run_episode({"return_value": ok})
        self.assertEqual((reward, len(record["steps"])), (1.0, 4))
        self.assertEqual(len(self.events(urlopen)["agent_steps"]), 4)

    def run_main(self, argv, vlm):
        tel = telemetry.TinybirdTelemetry("p.test-token", "https://tinybird.test", backoff=0)
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"{}"
        env = FakeEnv()
        with mock.patch.object(agent, "generate", vlm.generate), \
                mock.patch.object(agent, "load", return_value=(SimpleNamespace(config=None), SimpleNamespace(tokenizer=None))), \
                mock.patch.object(agent.gym, "make", return_value=env), mock.patch.object(telemetry, "from_env", return_value=tel), \
                mock.patch.object(telemetry._opener, "open", return_value=ok) as urlopen, \
                mock.patch("sys.argv", ["agent.py", *argv]), contextlib.redirect_stdout(io.StringIO()):
            self.urlopen = urlopen  # still readable when main() raises
            try:
                agent.main()
            finally:
                self.assertTrue(env.closed)
        return tel, urlopen

    def test_main_runs_several_episodes(self):
        tel, urlopen = self.run_main(["--episodes", "2", "--max-steps", "6", "--learn", "--seed", "3"], FakeVLM())
        ev = self.events(urlopen)
        (run,) = ev["agent_runs"]
        self.assertEqual((run["run_id"], run["model"], run["learning_enabled"], run["temperature"], run["seed"], run["episodes"], run["max_steps"]),
                         (agent.RUN_ID, agent.MODEL, True, 0.0, 3, 2, 6))
        results = ev["episode_results"]
        self.assertEqual([(r["episode_id"], r["episode_index"], r["seed"]) for r in results],
                         [(f"{agent.RUN_ID}-000", 0, 3), (f"{agent.RUN_ID}-001", 1, 4)])
        second = [s for s in ev["agent_steps"] if s["episode_index"] == 1]
        self.assertEqual([(s["episode_id"], s["seed"], s["step_index"]) for s in second],
                         [(f"{agent.RUN_ID}-001", 4, t) for t in range(4)])
        self.assertEqual(ev["agent_episodes"][1]["active_lesson_ids"], [telemetry.lesson_id(NEW_LESSON)])  # learned in episode 0
        self.assertTrue(all(c["episode_id"] == f"{agent.RUN_ID}-001" for c in ev["model_calls"] if c["episode_index"] == 1))
        self.assertNotIn("agent_errors", ev)
        self.assertEqual(tel.buffer, {})
        self.assertIn("sent", self.stderr.getvalue())

    def test_ctrl_c_is_recorded_and_still_raised(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_main(["--episodes", "3", "--max-steps", "6"], FakeVLM(interrupt_at=6))  # step 1 of episode 1
        (error,) = self.events(self.urlopen)["agent_errors"]
        self.assertEqual((error["component"], error["error_type"], error["episode_index"]), ("run", "KeyboardInterrupt", 1))

    def test_model_call_errors_are_recorded_and_still_raised(self):
        tel = telemetry.TinybirdTelemetry("p.test-token", "https://tinybird.test")
        agent.TELEMETRY = tel
        with mock.patch.object(agent, "generate", return_value=SimpleNamespace(text="{not json")), self.assertRaises(json.JSONDecodeError):
            agent.ask(SimpleNamespace(config=None), SimpleNamespace(tokenizer=None), "prompt", [], agent.ACTION_SCHEMA, 10, call_type="policy")
        (row,) = [json.loads(l) for l in tel.buffer["agent_errors"]]
        self.assertEqual((row["component"], row["error_type"]), ("policy", "JSONDecodeError"))


if __name__ == "__main__":
    unittest.main()
