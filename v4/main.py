"""v4: natural-language flight question -> Nimble picks the website -> on-device VLM browses it.

    NIMBLE_API_KEY=... RAWTREE_API_KEY=... uv run main.py "Give me the cheapest flights from LA to SF on the 26th of September"
    uv run main.py "..." --url https://www.kayak.com      # skip step A

A: Nimble web search for the question; the local VLM picks which result to open.
B: loop: B1 load page, B2 screenshot -> VLM -> action, B3 do it, B4 send the step trace to RawTree.
Screenshots go to runs/<run_id>/. Traces are skipped when RAWTREE_API_KEY is unset.
Optional NIMBLE_PROXY=http://account-...:pass@ip.nimbleway.com:7000 routes the browser through Nimble.
"""

import argparse
import datetime
import io
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from mlx_vlm import apply_chat_template, generate, load
from mlx_vlm.structured import build_json_schema_logits_processor
from PIL import Image
from playwright.sync_api import sync_playwright

MODEL = "LiquidAI/LFM2.5-VL-3B-MLX-8bit"
VIEW = {"width": 1280, "height": 800}
# headless Chromium says "HeadlessChrome" in its UA, which bot walls block on sight
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
RUN_ID = time.strftime("%Y%m%d-%H%M%S")

PICK_PROMPT = """Which search result is a website where you can search for flights and see prices for this request?

Request: {task}

Results:
{results}

Answer in JSON: {{"pick": <result number>}}"""

PROMPT = """You control a web browser by looking at a screenshot. Complete the task.
Today is {today}.

Task: {task}

Actions done so far:
{history}

Pick the next action. Answer in JSON:
"thought": what you see and why you pick this action,
"action": "click", "type", "enter", "scroll" or "done",
"arg": for click, what to click; for type, the text; for scroll, "up" or "down";
for done, the answer to the task (flights with times and prices you see).
Click a field before typing into it. After typing a city, click the matching suggestion.
Use "done" only when flight results with prices are visible."""

ACTION = {
    "type": "object",
    "properties": {
        "thought": {"type": "string", "maxLength": 200},
        "action": {"enum": ["click", "type", "enter", "scroll", "done"]},
        "arg": {"type": "string", "maxLength": 300},
    },
    "required": ["thought", "action", "arg"],
}
BBOX = {
    "type": "object",
    "properties": {"bbox": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 1000},
                            "minItems": 4, "maxItems": 4}},
    "required": ["bbox"],
}


def ask(model, processor, prompt, schema, img=None, max_tokens=250):
    """One constrained-JSON call to the local VLM. No image = text-only question."""
    images = [img] if img else []
    chat = apply_chat_template(processor, model.config, prompt, num_images=len(images))
    out = generate(model, processor, chat, image=images or None, max_tokens=max_tokens, temperature=0.0, verbose=False,
                   logits_processors=[build_json_schema_logits_processor(processor.tokenizer, schema)])
    return json.loads(getattr(out, "text", out))


def post(url, body, key):
    req = urllib.request.Request(url, json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def trace(table, **row):
    """B4: one row into RawTree (POST /v1/tables/<table>). Never kills the run."""
    key = os.environ.get("RAWTREE_API_KEY")
    if not key:
        return
    base = os.environ.get("RAWTREE_BASE_URL", "https://api.rawtree.com")
    try:
        post(f"{base}/v1/tables/{table}", [{"run_id": RUN_ID, "ts": time.time(), **row}], key)
    except Exception as e:  # ponytail: one blocking POST per step; batch per run if latency shows up
        print(f"  (rawtree trace failed: {e})", file=sys.stderr)


def search(task):
    """Step A: Nimble web search -> [(title, url)]."""
    body = post("https://sdk.nimbleway.com/v2/search",
                {"query": task, "max_results": 8, "country": "US", "locale": "en"}, os.environ.get("NIMBLE_API_KEY") or os.environ["NIMBLE_KEY"])
    items = body.get("results") or body.get("organic_results") or body.get("data") or []
    if isinstance(items, dict):  # some responses nest the list one level down
        items = items.get("results") or items.get("organic") or []
    return [(i.get("title", ""), i.get("url") or i.get("link")) for i in items if i.get("url") or i.get("link")]


def pick_site(model, processor, task):
    results = search(task)
    if not results:
        sys.exit("Nimble search returned no results")
    listing = "\n".join(f"{n}. {title} ({urlparse(url).netloc})" for n, (title, url) in enumerate(results, 1))
    print(f"search results:\n{listing}")
    schema = {"type": "object", "properties": {"pick": {"type": "integer", "minimum": 1, "maximum": len(results)}},
              "required": ["pick"]}
    n = ask(model, processor, PICK_PROMPT.format(task=task, results=listing), schema, max_tokens=20)["pick"]
    trace("v4_site_picks", task=task, results=[u for _, u in results], pick=n, url=results[n - 1][1])
    return results[n - 1][1]


def proxy():
    u = urlparse(os.environ["NIMBLE_PROXY"]) if os.environ.get("NIMBLE_PROXY") else None
    return u and {"server": f"{u.scheme}://{u.hostname}:{u.port}", "username": u.username, "password": u.password}


def act(page, model, processor, img, kind, arg):
    """B3: run one action on the page, return a log line."""
    if kind == "click":
        x1, y1, x2, y2 = ask(model, processor, f"Detect {arg}. Output its bounding box as JSON.", BBOX, img, 60)["bbox"]
        x, y = (x1 + x2) / 2000 * VIEW["width"], (y1 + y2) / 2000 * VIEW["height"]
        page.mouse.click(x, y)
        return f'click("{arg}") at ({x:.0f},{y:.0f})'
    if kind == "type":
        page.keyboard.type(arg, delay=80)  # per-key delay so autocompletes fire
        return f'type("{arg}")'
    if kind == "enter":
        page.keyboard.press("Enter")
        return "enter"
    if kind == "scroll":
        page.mouse.wheel(0, -500 if arg == "up" else 500)
        return f'scroll("{arg}")'
    return f'done("{arg}")'


def main():
    p = argparse.ArgumentParser()
    p.add_argument("task", nargs="?", default="Give me the cheapest flights from LA to SF on the 26th of September")
    p.add_argument("--url", help="skip the Nimble search and start here")
    p.add_argument("--max-steps", type=int, default=30)
    a = p.parse_args()

    out_dir = Path("runs") / RUN_ID
    out_dir.mkdir(parents=True, exist_ok=True)
    model, processor = load(MODEL)

    url = a.url or pick_site(model, processor, a.task)
    print(f"task: {a.task}\nsite: {url}\nproxy: {'nimble' if proxy() else 'none'}")
    trace("v4_runs", task=a.task, url=url, model=MODEL)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, proxy=proxy())
        page = browser.new_page(viewport=VIEW, user_agent=UA)
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)  # B1
        page.wait_for_timeout(3000)

        history, answer = [], None
        for t in range(a.max_steps):
            t0 = time.time()
            img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")  # B2
            shot = out_dir / f"{t:02d}.png"
            img.save(shot)
            prompt = PROMPT.format(today=datetime.date.today().isoformat(), task=a.task,
                                   history="\n".join(history) or "(none)")
            d = ask(model, processor, prompt, ACTION, img)
            line = act(page, model, processor, img, d["action"], d["arg"])  # B3
            print(f"\n[{t:02d}] {time.time() - t0:.1f}s  {page.url[:90]}\n  thought: {d['thought']}\n  action:  {line}")
            trace("v4_steps", step=t, page_url=page.url, thought=d["thought"], action=d["action"], arg=d["arg"],
                  result=line, screenshot=str(shot), latency_ms=round((time.time() - t0) * 1000))  # B4
            history.append(line)
            if d["action"] == "done":
                answer = d["arg"]
                break
            page.wait_for_timeout(2000)

        page.screenshot(path=out_dir / "final.png")
        browser.close()

    trace("v4_results", task=a.task, url=url, answer=answer, steps=len(history), success=answer is not None)
    print(f"\nanswer: {answer or '(ran out of steps)'}\nscreenshots: {out_dir}")


if __name__ == "__main__":
    main()
