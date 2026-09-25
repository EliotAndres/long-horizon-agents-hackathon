"""natural-language flight question -> Nimble picks the website -> on-device VLM browses it.

    NIMBLE_API_KEY=... RAWTREE_API_KEY=... uv run main.py "Give me the cheapest flights from LA to SF on the 26th of September"
    uv run main.py "..." --url https://www.kayak.com      # skip step A
    uv run main.py "..." --learn                          # use + update lessons in runs/lessons.json (learning.py)

A: Nimble web search for the question; the local VLM picks which result to open.
B: loop: B1 load page, B2 screenshot -> VLM -> action, B3 do it, B4 send the step trace to RawTree.
Screenshots and a captioned replay.mp4 (1.5s per step, red circle = click) go to runs/<run_id>/. Traces are skipped when RAWTREE_API_KEY is unset.
Optional NIMBLE_PROXY=http://account-...:pass@ip.nimbleway.com:7000 routes the browser through Nimble.
"""

import argparse
import datetime
import io
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from mlx_vlm import apply_chat_template, generate, load
from mlx_vlm.structured import build_json_schema_logits_processor
from PIL import Image, ImageChops, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

import learning

MODEL = "LiquidAI/LFM2.5-VL-3B-MLX-8bit"
VIEW = {"width": 1280, "height": 800}
# headless Chromium says "HeadlessChrome" in its UA, which bot walls block on sight
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
RUN_ID = time.strftime("%Y%m%d-%H%M%S")
FONT = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 20)
BOLD = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 22)
MODEL_MS = {}  # model time per call kind (policy, grounding, popup_check, ...) since the last step, for --learn traces

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
If your last action did NOT change the screen, do not repeat it; try a different action.
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
POPUP_PROMPT = 'Is a popup or dialog window open on top of the page, covering part of it? Answer in JSON: {"popup": "yes" or "no"}'
POPUP = {"type": "object", "properties": {"popup": {"enum": ["yes", "no"]}}, "required": ["popup"]}
BBOX = {
    "type": "object",
    "properties": {"bbox": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 1000},
                            "minItems": 4, "maxItems": 4}},
    "required": ["bbox"],
}


def ask(model, processor, prompt, schema, img=None, max_tokens=250, kind="policy"):
    """One constrained-JSON call to the local VLM. No image = text-only question."""
    t0 = time.time()
    images = [img] if img else []
    chat = apply_chat_template(processor, model.config, prompt, num_images=len(images))
    out = generate(model, processor, chat, image=images or None, max_tokens=max_tokens, temperature=0.0, verbose=False,
                   logits_processors=[build_json_schema_logits_processor(processor.tokenizer, schema)])
    MODEL_MS[kind] = MODEL_MS.get(kind, 0) + round((time.time() - t0) * 1000)
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
    n = ask(model, processor, PICK_PROMPT.format(task=task, results=listing), schema, max_tokens=20, kind="site_pick")["pick"]
    trace("v4_site_picks", task=task, results=[u for _, u in results], pick=n, url=results[n - 1][1])
    return results[n - 1][1]


def proxy():
    u = urlparse(os.environ["NIMBLE_PROXY"]) if os.environ.get("NIMBLE_PROXY") else None
    return u and {"server": f"{u.scheme}://{u.hostname}:{u.port}", "username": u.username, "password": u.password}


def find_popup_close(model, processor, img):
    """Atomic check before each step: is a popup covering the page? -> where its close button (X) is, else None.
    The 3B model over-says "yes"; a real X comes back as a small box, a false alarm as empty or full-screen."""
    if ask(model, processor, POPUP_PROMPT, POPUP, img, 20, "popup_check")["popup"] != "yes":
        return None
    x1, y1, x2, y2 = ask(model, processor, "Detect the close button (X) of the popup. Output its bounding box as JSON.",
                         BBOX, img, 60, "popup_check")["bbox"]
    if not (0 < x2 - x1 < 100 and 0 < y2 - y1 < 100):  # ponytail: a close button is <10% of the screen each way
        return None
    return (x1 + x2) / 2000 * VIEW["width"], (y1 + y2) / 2000 * VIEW["height"]


def act(page, model, processor, img, kind, arg):
    """B3: run one action on the page, return a log line."""
    if kind == "click":
        x1, y1, x2, y2 = ask(model, processor, f"Detect {arg}. Output its bounding box as JSON.", BBOX, img, 60,
                             "grounding")["bbox"]
        x, y = (x1 + x2) / 2000 * VIEW["width"], (y1 + y2) / 2000 * VIEW["height"]
        page.mouse.click(x, y)
        return f'click("{arg}") at ({x:.0f},{y:.0f})', (x, y)
    if kind == "type":
        page.keyboard.type(arg, delay=80)  # per-key delay so autocompletes fire
        return f'type("{arg}")', None
    if kind == "enter":
        page.keyboard.press("Enter")
        return "enter", None
    if kind == "scroll":
        page.mouse.wheel(0, -500 if arg == "up" else 500)
        return f'scroll("{arg}")', None
    return f'done("{arg}")', None


def screenshot(page):
    return Image.open(io.BytesIO(page.screenshot())).convert("RGB")


def unchanged(a, b):
    # ponytail: <500 px differing by >30 levels counts as no change (ignores cursors, tiny animations)
    return ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 30 else 0).histogram()[255] < 500


def frame(img, t, url, line, thought, pt):
    """Screenshot with a red circle on the click and a caption panel underneath."""
    img = img.copy()
    d = ImageDraw.Draw(img)
    if pt:
        x, y = pt
        d.ellipse((x - 22, y - 22, x + 22, y + 22), outline="red", width=5)
        d.ellipse((x - 6, y - 6, x + 6, y + 6), fill="red")
    out = Image.new("RGB", (img.width, img.height + 150), "black")
    out.paste(img)
    d = ImageDraw.Draw(out)
    d.text((16, img.height + 10), f"step {t}   {url[:90]}", font=FONT, fill="#aaa")
    d.text((16, img.height + 40), f"=> {line}"[:110], font=BOLD, fill="#ff6060")
    d.multiline_text((16, img.height + 72), textwrap.fill("thinks: " + thought, 120), font=FONT, fill="white")
    return out


def save_video(frames, path):
    frames[0].save(f"{path}.gif", save_all=True, append_images=frames[1:], duration=1500, loop=0)
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{path}.gif", "-movflags", "faststart",
                        "-pix_fmt", "yuv420p", "-vf", "fps=10,scale=trunc(iw/2)*2:trunc(ih/2)*2", f"{path}.mp4"], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass  # the gif is still there


def main():
    p = argparse.ArgumentParser()
    p.add_argument("task", nargs="?", default="Give me the cheapest flights from LA to SF on the 26th of September")
    p.add_argument("--url", help="skip the Nimble search and start here")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--learn", action="store_true", help="show lessons from runs/lessons.json to the policy, reflect after the run to update them")
    a = p.parse_args()

    out_dir = Path("runs") / RUN_ID
    out_dir.mkdir(parents=True, exist_ok=True)
    lessons = learning.load() if a.learn else None
    used = learning.active(lessons) if a.learn else None
    learn = {"learning": True, "active_lesson_ids": [l["id"] for l in used]} if a.learn else {}
    model, processor = load(MODEL)

    url = a.url or pick_site(model, processor, a.task)
    print(f"task: {a.task}\nsite: {url}\nproxy: {'nimble' if proxy() else 'none'}")
    if a.learn:
        print(f"lessons: using {len(used)} of {len(lessons)}", *(f"  - [{l['id']}] {l['text']}" for l in used), sep="\n")
    trace("v4_runs", task=a.task, url=url, model=MODEL, **learn)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, proxy=proxy())
        page = browser.new_page(viewport=VIEW, user_agent=UA)
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)  # B1
        page.wait_for_timeout(3000)
        answer, history, frames = run(page, model, processor, a.task, out_dir, a.max_steps, used)
        browser.close()

    if a.learn:
        learn["no_op_steps"] = sum(h.endswith("NOT change") for h in history)
    trace("v4_results", task=a.task, url=url, answer=answer, steps=len(history), success=answer is not None, **learn)
    print(f"\nanswer: {answer or '(ran out of steps)'}\nscreenshots + replay.mp4: {out_dir}")
    if a.learn:
        reflect(model, processor, a.task, answer, history, frames, used)


def reflect(model, processor, task, answer, history, frames, used):
    """--learn: the VLM reviews the run (steps + screenshot grid) and updates runs/lessons.json. Never kills the run."""
    t0, lessons, added, removed, error = time.time(), [], [], [], None
    try:
        out = ask(model, processor, learning.reflect_prompt(task, answer, history, used), learning.schema(used),
                  learning.contact_sheet(frames), 400, "reflection")
        lessons = learning.load()  # re-read: another --learn run may have saved since this one started
        added, removed = learning.apply(lessons, used, out, task, RUN_ID)
        learning.save(lessons)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"  (reflection failed: {error})", file=sys.stderr)
    for tag, ls in (("+ lesson", added), ("x removed", removed)):
        for l in ls:
            print(f"{tag} [{l['id']}] {l['text']}")
    trace("v4_reflections", task=task, success=answer is not None, steps=len(history),
          no_op_steps=sum(h.endswith("NOT change") for h in history), active_lesson_ids=[l["id"] for l in used],
          lessons_added_ids=[l["id"] for l in added], lessons_added=[l["text"] for l in added],
          lessons_removed_ids=[l["id"] for l in removed], lessons_removed=[l["text"] for l in removed],
          lessons_active_after=sum(l["active"] for l in lessons), latency_ms=round((time.time() - t0) * 1000), error=error)


def run(page, model, processor, task, out_dir, max_steps, lessons=None):
    """Step B on an already-loaded page. `lessons` (--learn only) go into the policy prompt.
    Returns (answer or None, history lines, captioned frames)."""
    history, answer, frames, check_popup = [], None, [], True
    img = screenshot(page)
    for t in range(max_steps):
        t0 = time.time()  # B2: img is the current screen
        MODEL_MS.clear()
        shot = out_dir / f"{t:02d}.png"
        img.save(shot)
        seen, url_before = img, page.url
        pt = check_popup and find_popup_close(model, processor, img)
        if pt:
            page.mouse.click(*pt)
            d = {"thought": "A popup covers the page, so I close it first.", "action": "close_popup", "arg": ""}
            line = f"close popup at ({pt[0]:.0f},{pt[1]:.0f})"
        else:
            prompt = PROMPT.format(today=datetime.date.today().isoformat(), task=task,
                                   history="\n".join(history) or "(none)") + learning.prompt_block(lessons or ())
            d = ask(model, processor, prompt, ACTION, img)
            line, pt = act(page, model, processor, img, d["action"], d["arg"])  # B3
        if d["action"] != "done":
            page.wait_for_timeout(2000)
            img = screenshot(page)
            if unchanged(seen, img):
                line += " -> the screen did NOT change"
        frames.append(frame(seen, t, url_before, line, d["thought"], pt))
        print(f"\n[{t:02d}] {time.time() - t0:.1f}s  {page.url[:90]}\n  thought: {d['thought']}\n  action:  {line}")
        learn = {} if lessons is None else {"active_lesson_ids": [l["id"] for l in lessons],
                                            "no_op": line.endswith("NOT change"), "model_ms": dict(MODEL_MS)}
        trace("v4_steps", step=t, page_url=page.url, thought=d["thought"], action=d["action"], arg=d["arg"],
              result=line, screenshot=str(shot), latency_ms=round((time.time() - t0) * 1000), **learn)  # B4
        history.append(line)
        # a "close" that changed nothing was a false alarm: let the policy act next step instead of looping on it
        check_popup = not (d["action"] == "close_popup" and line.endswith("NOT change"))
        if d["action"] == "done":
            answer = d["arg"]
            break

    page.screenshot(path=out_dir / "final.png")
    save_video(frames, out_dir / "replay")
    return answer, history, frames


if __name__ == "__main__":
    main()
