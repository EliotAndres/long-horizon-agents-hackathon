"""No-op guard, grounding validation and vague-target rejection in main.py. No network, no model, no browser.

    uv run python -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

try:
    import main
except (ImportError, OSError) as e:  # main.py needs mlx-vlm, playwright and a macOS font
    raise unittest.SkipTest(f"main.py not importable here: {e}") from e

GOOD = [400, 300, 460, 340]  # a button-sized box; its centre is (550, 256) in page pixels
BLACK, WHITE, GRAY = (Image.new("RGB", (1280, 800), c) for c in ("black", "white", "gray"))


class FakePage:
    url = "https://app.example"

    def __init__(self):
        self.mouse = mock.Mock()
        self.keyboard = mock.Mock()

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None):
        pass


def fake_vlm(actions, boxes=()):
    """Policy calls return `actions` in order; grounding calls return `boxes` in order, then GOOD."""
    calls, actions, boxes = [], iter(actions), iter(boxes)

    def ask(model, processor, prompt, schema, img=None, max_tokens=250, kind="policy"):
        calls.append((kind, prompt, schema))
        if kind == "grounding":
            return {"bbox": next(boxes, GOOD)}
        action, arg = next(actions)
        return {"thought": "t", "action": action, "arg": arg}
    return ask, calls


def run_agent(actions, screens, max_steps=5):
    ask, calls = fake_vlm(actions)
    page = FakePage()
    with tempfile.TemporaryDirectory() as d, mock.patch.object(main, "ask", ask), \
            mock.patch.object(main, "screenshot", side_effect=screens), \
            mock.patch.object(main, "save_video"), mock.patch.object(main, "trace"):
        answer, history, _ = main.run(page, None, None, "a task", Path(d), max_steps)
    return answer, history, calls, page


class NoOpGuard(unittest.TestCase):
    def test_repeat_on_the_same_screen_is_rejected_then_a_different_kind_is_forced(self):
        answer, history, calls, page = run_agent(
            [("click", "Search button"),  # step 0: runs, screen does not change
             ("click", "the search button"), ("click", "Search button"),  # step 1: rejected, and rejected again
             ("scroll", "down"),  # step 1: forced to another kind of action
             ("done", "ok")], [BLACK, BLACK, GRAY])
        self.assertEqual(page.mouse.click.call_count, 1)  # the repeated click never ran
        page.mouse.wheel.assert_called_once_with(0, 500)
        self.assertEqual(main.STATS["rejected"], 2)
        policy = [c for c in calls if c[0] == "policy"]
        self.assertIn('Your action click("the search button") was REJECTED: it already did nothing on this same screen',
                      policy[2][1])
        self.assertEqual(policy[2][2]["properties"]["action"]["enum"], ["click", "type", "enter", "scroll"])  # no done
        self.assertEqual(policy[3][2]["properties"]["action"]["enum"], ["type", "enter", "scroll"])  # no click, no done
        self.assertTrue(history[1].startswith('[rejected click("the search button"): '))
        self.assertTrue(history[1].endswith('scroll("down")'))
        self.assertEqual(answer, "ok")

    def test_same_target_failing_twice_is_rejected_on_any_screen(self):
        tried = [(BLACK, ("click", "search button"), True), (WHITE, ("click", "search button"), True)]
        self.assertEqual(main.rejected({"action": "click", "arg": "Search button"}, GRAY, tried), "no-op twice")
        self.assertIsNone(main.rejected({"action": "click", "arg": "Search button"}, GRAY, tried[:1]))
        self.assertIsNone(main.rejected({"action": "click", "arg": "Done button"}, BLACK, tried))
        scrolls = [(BLACK, ("scroll", "down"), True), (WHITE, ("scroll", "down"), True)]
        self.assertIsNone(main.rejected({"action": "scroll", "arg": "down"}, GRAY, scrolls))  # two-strike rule: clicks only
        self.assertEqual(main.rejected({"action": "scroll", "arg": "page down"}, BLACK, scrolls), "no-op here")  # same effect

    def test_when_the_forced_answer_is_refused_too_nothing_runs(self):
        main.STATS.clear()
        tried = [(BLACK, ("click", "search button"), True), (BLACK, ("scroll", "down"), True), (BLACK, ("enter", ""), True)]
        ask, calls = fake_vlm([("click", "Search button"), ("scroll", "down"), ("enter", "")])
        with mock.patch.object(main, "ask", ask):
            d, notes = main.decide(None, None, "prompt", BLACK, tried)
        self.assertEqual(d["action"], "wait")
        self.assertEqual(calls[2][2]["properties"]["action"]["enum"], ["type", "enter"])  # no click, scroll or done
        self.assertEqual((len(notes), main.STATS["rejected"]), (3, 3))
        page = FakePage()
        self.assertEqual(main.act(page, None, None, BLACK, "wait", ""), ("no action: every proposal was refused", None))
        self.assertEqual(page.mouse.method_calls + page.keyboard.method_calls, [])

    def test_an_action_that_leads_back_to_the_same_screen_is_rejected(self):
        # a round-trip calendar: each click on the same day flips between two screens
        answer, history, calls, page = run_agent(
            [("click", "26 in the calendar"), ("click", "26 in the calendar"),  # BLACK -> WHITE -> BLACK
             ("click", "26 in the calendar"), ("click", "Done button"),  # rejected: already ran on BLACK
             ("done", "ok")], [BLACK, WHITE, BLACK, GRAY])
        self.assertEqual(page.mouse.click.call_count, 3)
        self.assertEqual(main.STATS["rejected"], 1)
        self.assertIn('click("26 in the calendar"): loop]', history[2])
        self.assertIn('click("Done button") at', history[2])
        self.assertEqual(answer, "ok")


class Grounding(unittest.TestCase):
    def test_box_ok(self):
        for bad in ([0, 0, 0, 0], [0, 0, 2, 65], [0, 0, 1000, 1000], [0, 0, 20, 20], [500, 500, 499, 510],
                    [100, 100, 900, 900], [995, 400, 1000, 420], [0, 0, 25, 65], [10, 400, 20, 420], [0, 0, 500, 501]):
            self.assertFalse(main.box_ok(bad), bad)
        for good in (GOOD, [20, 20, 40, 40], [0, 460, 1000, 520], [0, 0, 500, 500]):  # icon, full-width bar, area limit
            self.assertTrue(main.box_ok(good), good)

    def test_bad_box_is_retried_once_then_never_clicked(self):
        main.STATS.clear()
        page = FakePage()
        ask, calls = fake_vlm([], boxes=[[0, 0, 0, 0], [0, 0, 1000, 1000]])
        with mock.patch.object(main, "ask", ask):
            line, pt = main.act(page, None, None, BLACK, "click", "Where to? input field")
        page.mouse.click.assert_not_called()
        self.assertEqual((line, pt), ('click("Where to? input field") NOT done: could not locate it on the screen', None))
        self.assertEqual((len(calls), main.STATS["invalid_grounding"]), (2, 2))
        self.assertIn("visible Where to? input field element", calls[1][1])

        ask, _ = fake_vlm([], boxes=[[0, 0, 2, 65]])  # bad, then GOOD on the retry
        with mock.patch.object(main, "ask", ask):
            line, pt = main.act(page, None, None, BLACK, "click", "Where to? input field")
        page.mouse.click.assert_called_once_with(550.4, 256.0)
        self.assertEqual(main.STATS["invalid_grounding"], 3)


class VagueTargets(unittest.TestCase):
    def test_vague_click_targets_are_rejected(self):
        for arg in ("date", "The Date", "calendar", "down", "results", "", '"date"'):
            self.assertEqual(main.rejected({"action": "click", "arg": arg}, BLACK, []), "vague", arg)
        for action, arg in (("click", "26 in the calendar"), ("click", "Search"), ("click", "Where to? input field"),
                            ("click", "› button"), ("click", "+ button"), ("scroll", "down"), ("type", "date")):
            self.assertIsNone(main.rejected({"action": action, "arg": arg}, BLACK, []), (action, arg))
        self.assertNotEqual(main.key({"action": "type", "arg": "東京"}), main.key({"action": "type", "arg": "大阪"}))
        self.assertEqual(main.key({"action": "click", "arg": "the Search button!"}), ("click", "search button"))


class NormalAction(unittest.TestCase):
    def test_a_good_click_runs_once_with_no_extra_calls(self):
        answer, history, calls, page = run_agent([("click", "Search button"), ("done", "found it")], [BLACK, WHITE])
        page.mouse.click.assert_called_once_with(550.4, 256.0)
        self.assertEqual(history, ['click("Search button") at (550,256)', 'done("found it")'])
        self.assertEqual([c[0] for c in calls], ["policy", "grounding", "policy"])
        self.assertEqual((main.STATS["rejected"], main.STATS["invalid_grounding"]), (0, 0))
        self.assertEqual(answer, "found it")


if __name__ == "__main__":
    unittest.main()
