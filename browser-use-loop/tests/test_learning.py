"""--learn: lesson ids, persistence, add/remove, bounded prompt injection, and that runs without --learn are unchanged
and never depend on RawTree or the reflection. No network, no model, no browser.

    uv run python -m unittest discover -s tests
"""

import contextlib
import datetime
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import learning

try:
    import main
except (ImportError, OSError) as e:  # main.py needs mlx-vlm, playwright and a macOS font
    raise unittest.SkipTest(f"main.py not importable here: {e}") from e

TASK = "Give me the cheapest flights from LA to SF on the 26th of September"
STEP_KEYS = {"step", "page_url", "thought", "action", "arg", "result", "screenshot", "latency_ms"}


def lesson(text, active=True):
    return {"id": learning.lesson_id(text), "text": text, "active": active, "added_run": "r0", "removed_run": None}


class FakePage:
    url = "https://flights.example"

    def __init__(self):
        self.mouse = mock.Mock()
        self.keyboard = mock.Mock()

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None):
        pass


class LessonStore(unittest.TestCase):
    def test_ids_are_stable_and_normalized(self):
        a = learning.lesson_id("If a popup covers the page, then close it.")
        self.assertEqual(a, learning.lesson_id("  if a POPUP covers   the page, then close it."))
        self.assertEqual(a, hashlib.sha256(b"if a popup covers the page, then close it.").hexdigest()[:16])
        self.assertRegex(a, r"^[0-9a-f]{16}$")
        self.assertNotEqual(a, learning.lesson_id("If a popup covers the page, then scroll."))

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "runs" / "lessons.json"
            self.assertEqual(learning.load(path), [])
            lessons = [lesson("If a popup covers the page, then close it."), lesson("If x, then y.", active=False)]
            learning.save(lessons, path)
            self.assertEqual(learning.load(path), lessons)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["lessons.json"])  # no temp file left behind

    def test_apply_adds_general_lessons_and_deactivates_blamed_ones(self):
        bad, kept, unused = (lesson("If the page is slow, then press enter again."), lesson("If a field is empty, then click it."),
                             lesson("If results show, then answer."))
        lessons, used = [unused, bad, kept], [bad, kept]
        good = "If typing did not change the page, then click the input field first."
        out = {"remove": [bad["id"], unused["id"], "not-an-id"], "lessons": [
            good, "  If typing did not change the page,  then click the input field first. ",  # duplicate after normalizing
            "If the fare is under 200, then pick it.",  # digits
            "If flying from LA, then pick the first result.",  # name from the task
            "If the date is in September, then open the calendar.",  # date word
            "If the date is in sep, then open the calendar.",  # abbreviated month
            "If kayak.com shows a banner, then close it.",  # site
            "If San Francisco appears in the suggestions, then click it.",  # city not in the task
            "If the origin shows LAX, then keep it.",  # airport code
            "If Google Flights shows a consent page, then accept it.",  # site name without a domain
            "If sf is suggested, then click it.",  # task name, lowercased
            "Click the search button.",  # not "If ..."
        ]}
        added, removed = learning.apply(lessons, used, out, TASK, "r1")
        self.assertEqual([l["text"] for l in added], [good])
        self.assertEqual(added[0]["id"], learning.lesson_id(good))
        self.assertEqual(removed, [bad])  # `unused` wasn't shown this run, so it can't be blamed
        self.assertEqual((bad["active"], bad["removed_run"]), (False, "r1"))
        self.assertTrue(unused["active"] and kept["active"])
        self.assertIn(bad, lessons)  # deactivated, not deleted: its id stays resolvable
        self.assertEqual(learning.active(lessons), [unused, kept, added[0]])
        self.assertEqual(learning.apply(lessons, used, {"remove": [], "lessons": [good]}, TASK, "r2"), ([], []))  # known id

    def test_secret_values_never_become_lessons(self):
        with mock.patch.dict(os.environ, {"RAWTREE_API_KEY": "sk_live_abcdefghij"}):
            self.assertFalse(learning.general("If asked for a key, then type sk_live_abcdefghij.", "find flights"))
            self.assertTrue(learning.general("If asked for a key, then close the dialog.", "find flights"))

    def test_active_is_bounded_to_the_most_recent(self):
        lessons = [lesson(f"If situation {chr(97 + i)}, then act.") for i in range(20)]
        lessons[-1]["active"] = False
        used = learning.active(lessons)
        self.assertEqual(len(used), learning.MAX_ACTIVE)
        self.assertEqual(used, lessons[-learning.MAX_ACTIVE - 1:-1])
        self.assertEqual(learning.prompt_block([]), "")

    def test_reflection_schema_only_allows_used_ids(self):
        used = [lesson("If a, then b.")]
        self.assertEqual(learning.schema(used)["properties"]["remove"]["items"], {"enum": [used[0]["id"]]})
        self.assertEqual(learning.schema([])["properties"]["remove"]["maxItems"], 0)

    def test_contact_sheet_is_bounded(self):
        frames = [Image.new("RGB", (1280, 950)) for _ in range(30)]
        self.assertEqual(learning.contact_sheet(frames).size, (1280, 3 * (320 * 950 // 1280)))  # 12 shots, 4 x 3


class Run(unittest.TestCase):
    """main.run with a fake page and a fake VLM: step 0 scrolls (screen unchanged), step 1 says done."""

    def run_agent(self, lessons):
        prompts, rows = [], []

        def fake_ask(model, processor, prompt, schema, img=None, max_tokens=250, kind="policy"):
            prompts.append(prompt)
            main.MODEL_MS[kind] = 5
            return {"thought": "t", "action": "scroll" if len(prompts) == 1 else "done", "arg": "down" if len(prompts) == 1 else "42"}

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(main, "ask", fake_ask), \
                mock.patch.object(main, "screenshot", side_effect=lambda page: Image.new("RGB", (1280, 800))), \
                mock.patch.object(main, "save_video"), \
                mock.patch.object(main, "trace", side_effect=lambda table, **row: rows.append((table, row))):
            answer, history, frames = main.run(FakePage(), None, None, TASK, Path(d), 5, lessons)
        return answer, history, frames, prompts, rows

    def test_without_learn_prompt_and_traces_are_unchanged(self):
        answer, history, frames, prompts, rows = self.run_agent(None)
        self.assertEqual(answer, "42")
        self.assertEqual(history, ['scroll("down") -> the screen did NOT change', 'done("42")'])
        self.assertEqual(len(frames), 2)
        today = datetime.date.today().isoformat()
        self.assertEqual(prompts, [main.PROMPT.format(today=today, task=TASK, history="(none)", learnings=""),
                                   main.PROMPT.format(today=today, task=TASK, history=history[0], learnings="")])
        self.assertEqual([(t, set(r)) for t, r in rows], [("v4_steps", STEP_KEYS)] * 2)

    def test_with_learn_prompt_gets_the_lessons_and_steps_record_them(self):
        used = learning.active([lesson(f"If situation {chr(97 + i)}, then act.") for i in range(12)])
        _, _, _, prompts, rows = self.run_agent(used)
        self.assertEqual(prompts[0].count("\nLesson from past runs: "), learning.MAX_ACTIVE)
        self.assertTrue(prompts[0].endswith(learning.prompt_block(used)))
        step = rows[0][1]
        self.assertEqual(set(step) - STEP_KEYS, {"active_lesson_ids", "no_op", "model_ms"})
        self.assertEqual((step["active_lesson_ids"], step["no_op"], step["model_ms"]), ([l["id"] for l in used], True, {"policy": 5}))
        self.assertFalse(rows[1][1]["no_op"])

    def test_learn_flag_defaults_off(self):
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), \
                mock.patch("sys.argv", ["main.py", TASK, "--url", "https://x"]), \
                mock.patch.object(main, "load", side_effect=SystemExit), mock.patch.object(learning, "load") as load:
            with self.assertRaises(SystemExit):
                main.main()
        load.assert_not_called()


class NeverBreaksTheRun(unittest.TestCase):
    def test_trace_swallows_rawtree_errors(self):
        with mock.patch.dict(os.environ, {"RAWTREE_API_KEY": "k"}), mock.patch.object(main, "post", side_effect=OSError("down")):
            self.assertIsNone(main.trace("v4_steps", step=0))

    def test_reflection_failure_is_traced_and_keeps_lessons(self):
        rows, lessons = [], [lesson("If a, then b.")]
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), \
                mock.patch.object(main, "ask", side_effect=RuntimeError("model crashed")), \
                mock.patch.object(main, "trace", side_effect=lambda table, **row: rows.append((table, row))):
            learning.save(lessons)
            before = learning.LESSONS_FILE.read_text()
            main.reflect(None, None, TASK, None, ["enter -> the screen did NOT change"], [Image.new("RGB", (64, 40))], lessons)
            self.assertEqual(learning.LESSONS_FILE.read_text(), before)
        table, row = rows[0]
        self.assertEqual((table, row["error"], row["no_op_steps"], row["success"]), ("v4_reflections", "RuntimeError: model crashed", 1, False))

    def test_reflection_saves_lessons_on_top_of_the_latest_file(self):
        rows, lessons = [], [lesson("If a, then b.")]
        other = lesson("If another run learned this, then keep it.")  # saved by an overlapping --learn run
        new = "If typing did not change the page, then click the input field first."
        with tempfile.TemporaryDirectory() as d, contextlib.chdir(d), \
                mock.patch.object(main, "ask", return_value={"remove": [lessons[0]["id"]], "lessons": [new]}), \
                mock.patch.object(main, "trace", side_effect=lambda table, **row: rows.append((table, row))):
            learning.save(lessons + [other])
            main.reflect(None, None, TASK, "42", ['done("42")'], [Image.new("RGB", (64, 40))], lessons)
            saved = json.loads(learning.LESSONS_FILE.read_text())
        self.assertEqual([(l["text"], l["active"]) for l in saved],
                         [("If a, then b.", False), (other["text"], True), (new, True)])
        row = rows[0][1]
        self.assertEqual((row["lessons_added_ids"], row["lessons_removed_ids"], row["error"]),
                         ([learning.lesson_id(new)], [lessons[0]["id"]], None))


if __name__ == "__main__":
    unittest.main()
