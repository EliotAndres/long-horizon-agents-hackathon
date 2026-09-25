"""Record a person browsing a real site as a trace, so the agent can learn from people.

Opens a visible Chromium window (Playwright). You browse normally (search a flight on Expedia, solve
a captcha if one shows up); every click, typed value and scroll is logged. Close the window when done.
The episode is appended to data/traces/trajectories.jsonl in the same format as
browser-use-loop/agent.py, so browser-use-loop/learn.py derives the playbook from it:

    python -m app.integrations.recorder "roundtrip flight SFO to Paris Oct 9-16" --url https://www.expedia.com/
    cd ../../browser-use-loop && uv run learn.py --runs ../horizonbook/data/traces

Passwords and payment fields are never recorded (only "<hidden>").
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.integrations import nimble
from app.integrations.browser import Browser

TRACES = nimble.HORIZONBOOK_DIR / "data" / "traces"
MIN_MUTATIONS = 5  # DOM changes after an action before we count it as "the screen changed"

# Runs in every page and frame; reports actions to Python through window.__hbRecord.
CAPTURE = r"""
(() => {
  if (window.__hbInstalled) return; window.__hbInstalled = true;
  let mutations = 0;
  new MutationObserver(m => { mutations += m.length; }).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  window.__hbPending = () => mutations;
  const handle = el => {
    const key = el.id || el.getAttribute("data-stid") || el.getAttribute("data-testid") || el.getAttribute("name") || "";
    return el.tagName.toLowerCase() + (key ? "#" + key.replace(/[^\w-]/g, "-").slice(0, 40) : "");
  };
  const secret = el => el.type === "password" || /cc-|card|cvv|cvc|password/i.test((el.autocomplete || "") + " " + (el.name || "") + " " + (el.id || ""));
  const label = el => (el.getAttribute("aria-label") || (el.labels && el.labels[0] && el.labels[0].innerText) || el.innerText
                       || el.placeholder || el.value || el.getAttribute("title") || "").replace(/\s+/g, " ").trim().slice(0, 60);
  const send = (ev) => { if (window.__hbRecord) window.__hbRecord({...ev, mutations, url: location.href, title: document.title}); mutations = 0; };
  document.addEventListener("click", e => {
    const el = e.target.closest("button,a,input,select,textarea,label,li,[role=button],[role=option],[role=tab],[role=link]") || e.target;
    send({type: "click", x: Math.round(e.clientX), y: Math.round(e.clientY), label: label(el), el: handle(el),
          text: secret(el) ? "" : (el.value || el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 30)});
  }, true);
  document.addEventListener("change", e => {
    const el = e.target;
    if (!("value" in el) || el.type === "checkbox" || el.type === "radio") return;
    send({type: "type", value: secret(el) ? "<hidden>" : String(el.value).slice(0, 80), el: handle(el), label: label(el)});
  }, true);
  document.addEventListener("keydown", e => { if (e.key === "Enter") send({type: "press", key: "Enter", el: handle(e.target)}); }, true);
  let lastScroll = 0;
  window.addEventListener("wheel", e => {
    if (Date.now() - lastScroll < 1500) return; lastScroll = Date.now();
    send({type: "scroll", dir: e.deltaY > 0 ? "down" : "up"});
  }, {capture: true, passive: true});
})();
"""


def _step(ev: dict) -> dict:
    """Browser event -> a step in agent.py's log format (learn.py parses these strings)."""
    state = f"{ev.get('title', '')} | {ev.get('url', '')}"[:200]
    if ev["type"] == "click":
        action = f'click("{ev["label"] or ev["el"]}")'
        return {"state": state, "action": action, "result": f'{action} -> clicked ({ev["x"]},{ev["y"]}) on {ev["el"]} "{ev["text"]}"'}
    if ev["type"] == "type":
        action = f'type("{ev["value"]}")'
        return {"state": state, "action": action, "result": f'{action} into {ev["el"]}'}
    if ev["type"] == "press":
        return {"state": state, "action": f'press("{ev["key"]}")', "result": f'press("{ev["key"]}") in {ev["el"]}'}
    if ev["type"] == "blocked":
        return {"state": state, "action": "wait()", "result": f"blocked by the site ({ev['why']}), a person has to solve it"}
    return {"state": state, "action": f'scroll("{ev["dir"]}")', "result": f'scroll("{ev["dir"]}")'}


def record(task: str, url: str, success: bool | None = None, out_dir: Path = TRACES, headless: bool = False,
           script=None) -> dict:
    """Record one episode. `script(browser)` drives the page instead of a person (for tests)."""
    steps: list[dict] = []
    last_url = [url]

    def on_event(_source, ev):
        # The DOM changes and URL change since the previous action tell us whether that action did something.
        if steps:
            steps[-1]["changed"] = ev.get("mutations", 0) >= MIN_MUTATIONS or ev.get("url") != last_url[0]
        last_url[0] = ev.get("url", last_url[0])
        steps.append({**_step(ev), "changed": True})
        print(f"  {len(steps) - 1:2d} {steps[-1]['result'][:110]}")

    with Browser(headless=headless) as b:
        b.context.expose_binding("__hbRecord", on_event)
        b.context.add_init_script(CAPTURE)
        b.goto(url)
        if (why := b.blocked()):
            on_event(None, {"type": "blocked", "why": why, "url": b.page.url, "title": b.page.title()})
        if script:
            script(b)
            b.page.wait_for_timeout(1000)
            if steps:
                steps[-1]["changed"] = b.page.evaluate("window.__hbPending()") >= MIN_MUTATIONS or b.page.url != last_url[0]
        else:
            print("Browse in the window. Close it when you're done.")
            while b.context.pages:  # the last step stays "changed": the person saw its result before closing
                try:
                    b.context.pages[0].wait_for_timeout(300)
                except Exception:
                    break

    if success is None:
        success = input("Did you get what you wanted? [y/n] ").strip().lower().startswith("y")
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "trajectories.jsonl"
    seed = sum(1 for _ in log.open()) if log.exists() else 0
    episode = {"run": time.strftime("%Y%m%d-%H%M%S"), "seed": seed, "task": task, "reward": 1.0 if success else -1.0,
               "steps": steps, "lessons_added": [], "lessons_removed": [], "source": "human", "start_url": url}
    with log.open("a") as f:
        f.write(json.dumps(episode) + "\n")
    print(f"recorded {len(steps)} steps -> {log}")
    return episode


def main() -> None:
    p = argparse.ArgumentParser(description="Record a person browsing, as a trace the agent learns from")
    p.add_argument("task", help='what you are trying to do, e.g. "roundtrip flight SFO to Paris Oct 9-16"')
    p.add_argument("--url", default="https://www.expedia.com/")
    p.add_argument("--success", choices=["y", "n"], help="skip the question at the end")
    a = p.parse_args()
    record(a.task, a.url, None if a.success is None else a.success == "y")


if __name__ == "__main__":
    main()
