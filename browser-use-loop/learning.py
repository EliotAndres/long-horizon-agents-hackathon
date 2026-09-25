"""--learn: general lessons from past runs go into the policy prompt; after each run the VLM reviews it and updates them.

Ported from archived/browser-use-loop (agent.py reflect/contact_sheet, telemetry.py lesson_id). Lessons live in
runs/lessons.json as {id, text, active, added_run, removed_run}. A removed lesson is only deactivated, so its id
stays resolvable in old traces.
"""

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LESSONS_FILE = Path("runs/lessons.json")
MAX_ACTIVE = 8  # lessons shown to the policy per run: the most recent active ones
FONT = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 28)

REFLECT_PROMPT = """You are reviewing an attempt by a web agent that browses real websites from screenshots, to help it do better next time.

Task: {task}
Outcome: {outcome}

The image is a grid of the screen before some of the steps (step number in red; red circle = where it clicked).
Steps (action -> result; "did NOT change" means the action did nothing):
{trace}

Lessons the agent was following (id: lesson):
{known}

First, list the ids of lessons that led the agent into mistakes in this attempt
(it followed the lesson and got stuck, or the screen did not change). These will be deleted.

Then write 1 to 3 NEW general lessons that will help on ANY future task on this kind of website.
Each lesson has the form "If <situation you see>, then <what to do>".
Do NOT mention specific cities, airports, dates, prices, numbers, website names or screen positions.
Good examples:
- If a cookie or sign-in popup covers the page, then close it before anything else.
- If you typed a city and suggestions appear, then click the matching suggestion before moving on.
- If the same action did not change the page, then try a different action.
Answer in JSON: {{"remove": [ids], "lessons": [new lessons]}}"""

SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASS|PROXY|CRED|AUTH", re.I)
DATE_WORDS = re.compile(r"\b(january|february|march|april|june|july|august|september|october|november|december|"
                        r"jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec|"
                        r"(mon|tues|wednes|thurs|fri|satur|sun)day|today|tomorrow|tonight)\b", re.I)
SITE = re.compile(r"https?:|www\.|\b[\w-]+\.(com|net|org|io|co|travel|fr|de|uk)\b", re.I)


def lesson_id(text):
    """Stable id of a lesson: sha256 of its normalized text (NFKC, casefolded, whitespace collapsed), 16 hex chars."""
    return hashlib.sha256(" ".join(unicodedata.normalize("NFKC", text).casefold().split()).encode()).hexdigest()[:16]


EXAMPLE_IDS = {lesson_id(l[2:]) for l in REFLECT_PROMPT.splitlines() if l.startswith("- If ")}


def load(path=LESSONS_FILE):
    return json.loads(path.read_text()) if path.exists() else []


def save(lessons, path=LESSONS_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(lessons, indent=1))
    tmp.replace(path)  # atomic: a crash mid-write never leaves a half-written lessons file


def active(lessons, limit=MAX_ACTIVE):
    return [l for l in lessons if l["active"]][-limit:]


def prompt_block(used):
    """Appended to the policy prompt. Empty when there are no lessons, so the prompt is unchanged."""
    return "".join(f"\nLesson from past runs: {l['text']}" for l in used)


def schema(used):
    """Reflection output. `remove` can only name lessons this run used. (A regex `pattern` on lessons derails
    this decoder, so their form is checked afterwards by general().)"""
    ids = [l["id"] for l in used]
    return {
        "type": "object",
        "properties": {
            "remove": {"type": "array", "items": {"enum": ids} if ids else {"type": "string"}, "maxItems": len(ids)},
            "lessons": {"type": "array", "minItems": 1, "maxItems": 3,
                        "items": {"type": "string", "maxLength": 160}},
        },
        "required": ["remove", "lessons"],
    }


def reflect_prompt(task, answer, history, used):
    outcome = f"SUCCESS, the agent answered: {answer}" if answer is not None else "FAILED, ran out of steps without an answer"
    return REFLECT_PROMPT.format(task=task, outcome=outcome, trace="\n".join(f"{i}: {h}" for i, h in enumerate(history)),
                                 known="\n".join(f"{l['id']}: {l['text']}" for l in used) or "(none)")


def contact_sheet(frames, cols=4, width=320, max_shots=12):
    """Up to 12 evenly spread step frames in one numbered grid, so the VLM sees the whole run in one image."""
    n = len(frames)
    picks = sorted({round(i * (n - 1) / (max_shots - 1)) for i in range(max_shots)}) if n > max_shots else range(n)
    h = width * frames[0].height // frames[0].width
    sheet = Image.new("RGB", (width * cols, h * ((len(picks) + cols - 1) // cols)), "white")
    for k, i in enumerate(picks):
        x, y = width * (k % cols), h * (k // cols)
        sheet.paste(frames[i].resize((width, h)), (x, y))
        ImageDraw.Draw(sheet).text((x + 6, y + 4), str(i), font=FONT, fill="red")
    return sheet


def general(text, task):
    """True if a lesson carries nothing from this one run: no digits, no capitalized names (cities, airport codes,
    sites, months) after the leading "If", no word from the task's names, no dates, sites or secrets."""
    names = {w.casefold() for w in re.findall(r"\b[A-Z][\w']+", task)} - {"if", "i"}
    secrets = [v for k, v in os.environ.items() if SECRET_ENV.search(k) and len(v) >= 8]
    return (text.startswith("If ") and not re.search(r"\d|\b[A-Z][\w']+", text[3:])
            and not names & {w.casefold() for w in re.findall(r"[\w']+", text)}
            and not DATE_WORDS.search(text) and not SITE.search(text) and not any(s in text for s in secrets)
            and lesson_id(text) not in EXAMPLE_IDS)


def apply(lessons, used, out, task, run_id):
    """Deactivate the used lessons the reflection blamed, add its new general ones. Returns (added, removed)."""
    blamed = set(out["remove"]) & {l["id"] for l in used}
    removed = [l for l in lessons if l["active"] and l["id"] in blamed]
    for l in removed:
        l.update(active=False, removed_run=run_id)
    known, added = {l["id"] for l in lessons}, []
    for text in out["lessons"][:3]:
        text = " ".join(text.split())
        if lesson_id(text) not in known and general(text, task):
            known.add(lesson_id(text))
            added.append({"id": lesson_id(text), "text": text, "active": True, "added_run": run_id, "removed_run": None})
        else:
            print(f"- dropped lesson (known or too specific): {text}")
    lessons.extend(added)
    return added, removed
