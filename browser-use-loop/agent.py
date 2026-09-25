"""Vision-only VLM browser agent for MiniWoB++ book-flight, running Liquid AI LFM2.5-VL on-device via MLX.

Each step the VLM sees only the screenshot + task + its past actions (no DOM), and:
  1. decides:  click("<what to click>") | type("<text>") | scroll("up"/"down")
  2. grounds a click target: asks the same VLM for its bounding box, clicks the center.
With --learn, after each episode it reviews the step screenshots (reflection) and writes lessons to
runs/lessons.json, which are fed into later episodes' prompts.

    uv run agent.py --episodes 5 --video           # headless, save captioned videos
    uv run agent.py --episodes 20 --learn          # learn from own attempts
    uv run agent.py --episodes 1 --show            # watch Chrome
"""

import argparse
import json
import re
import subprocess
import textwrap
import time
from pathlib import Path

import gymnasium as gym
import miniwob
import numpy as np
from miniwob.action import ActionTypes
from mlx_vlm import apply_chat_template, generate, load
from mlx_vlm.structured import build_json_schema_logits_processor
from PIL import Image, ImageDraw, ImageFont

gym.register_envs(miniwob)

MODEL = "LiquidAI/LFM2.5-VL-3B-MLX-8bit"
W, H = 160, 210  # MiniWoB task area in page pixels
SCALE = 3  # screenshot upscale for the VLM
LESSONS_FILE = Path("runs/lessons.json")
FONT = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)

PROMPT = """You control a web page by looking at the screenshot. Complete the task.

Task: {task}

Your previous actions (already done, do not redo them):
{history}
{lessons}
Answer in JSON:
"state": what you see (which fields are filled, what popup is open),
"action": one of "click", "type", "scroll",
"arg": for click, a short description of what to click; for type, the text; for scroll, "up" or "down".

How this site works: click an input, then type into it. Typing in From/To shows suggestions
right below; click the correct suggestion. Clicking the date input opens a calendar: use its
arrows to change month, then click the day. Then click Search. The results list may need
scroll("down"); click the "Book flight" button of the right flight."""

GROUND_PROMPT = 'Detect {target}. Output its bounding box as JSON: {{"bbox": [x1, y1, x2, y2]}}'

# Structured outputs: decoding is constrained to these JSON schemas (llguidance via mlx-vlm).
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "maxLength": 200},
        "action": {"enum": ["click", "type", "scroll"]},
        "arg": {"type": "string", "maxLength": 80},
    },
    "required": ["state", "action", "arg"],
}
COORD = {"type": "integer", "minimum": 0, "maximum": 1000}
BBOX_SCHEMA = {
    "type": "object",
    "properties": {"bbox": {"type": "array", "items": COORD, "minItems": 4, "maxItems": 4}},
    "required": ["bbox"],
}
LESSONS_SCHEMA = {
    "type": "object",
    "properties": {"lessons": {"type": "array", "items": {"type": "string", "maxLength": 160}, "minItems": 1, "maxItems": 3}},
    "required": ["lessons"],
}

REFLECT_PROMPT = """You are reviewing an attempt by a web agent, to help it do better next time.

Task: {task}
Outcome: {outcome}

The image is a grid of the screen before each step (step number in red; red dot = where it clicked).
Steps (action -> result):
{trace}

Lessons already known:
{known}

What went wrong or right? Write 1 to 3 NEW general lessons that will help on ANY future task
on this kind of website. Each lesson is reasoning of the form "if <situation you see> then <what to do>".
Do NOT mention specific cities, codes, dates, prices or screen positions.
Good examples:
- If a calendar is open, pick the date on it instead of typing.
- If you typed in a field and suggestions appear, click the matching suggestion before moving on.
- If the same action did not change the page, try a different action.
Answer in JSON: {{"lessons": [...]}}"""


EXAMPLE_LESSONS = {l[2:] for l in REFLECT_PROMPT.splitlines() if l.startswith("- If ")}


def ask(model, processor, prompt, image, schema, max_tokens, temperature=0.0):
    """One VLM call whose output is forced to match `schema`; returns the parsed dict."""
    chat = apply_chat_template(processor, model.config, prompt, num_images=1)
    constrain = build_json_schema_logits_processor(processor.tokenizer, schema)
    out = generate(model, processor, chat, image=[image], max_tokens=max_tokens, temperature=temperature,
                   logits_processors=[constrain], verbose=False)
    return json.loads(getattr(out, "text", out))


def big(screenshot):
    return Image.fromarray(screenshot).resize((W * SCALE, H * SCALE), Image.LANCZOS)


def ground(model, processor, img, target):
    """Bounding box of `target` on the screenshot -> click point in page pixels, or None."""
    x1, y1, x2, y2 = ask(model, processor, GROUND_PROMPT.format(target=target), img, BBOX_SCHEMA, 60)["bbox"]  # [0, 1000]
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) / 2000 * W, (y1 + y2) / 2000 * H


def prepare_episode(env):
    """MiniWoB doesn't reload the page between episodes, so the datepicker keeps focus, its calendar
    stays open, and red .error borders stay (the task's cleanup has typos). Clear them, lift the
    timer, and return a fresh obs."""
    # ponytail: book-flight has a 30s real-time limit; we lift it and score with raw_reward
    # (+1/-1, no time decay). Remove once the agent is fast enough.
    env.unwrapped.instance.driver.execute_script(
        "clearTimeout(core.EP_TIMER); core.EPISODE_MAX_TIME=1e9;"
        "core.EP_TIMER=setTimeout(function(){core.endEpisode(-1,false,'timed out')},1e9);"
        "document.activeElement.blur(); $('.hasDatepicker').datepicker('hide'); $('.error').removeClass('error');"
    )
    return env.step(env.unwrapped.create_action(ActionTypes.NONE))[0]


def element_at(env, x, y):
    """What's under the click, for the action log (feedback, not policy input)."""
    return env.unwrapped.instance.driver.execute_script(
        "var r=document.getElementById('wrap').getBoundingClientRect();"
        "var e=document.elementFromPoint(r.left+arguments[0], r.top+arguments[1]);"
        "return e ? (e.tagName.toLowerCase() + (e.id ? '#'+e.id : '') + ' \"' + (e.value || e.innerText || '').slice(0,30) + '\"') : 'nothing';",
        x, y,
    )


def execute(env, model, processor, action, img):
    """Run one parsed action. Returns (env_action or None, log line, click point or None)."""
    kind, arg = action
    ua = env.unwrapped
    if kind == "click":
        pt = ground(model, processor, img, arg)
        if pt is None:
            return None, f'click("{arg}") -> FAILED, could not find it on screen', None
        return ua.create_action(ActionTypes.CLICK_COORDS, coords=np.array(pt)), \
            f'click("{arg}") -> clicked ({pt[0]:.0f},{pt[1]:.0f}) on {element_at(env, *pt)}', pt
    if kind == "type":
        # Replace instead of append, like select-all + type.
        ua.instance.driver.execute_script("var e=document.activeElement; if(e && 'value' in e && !e.readOnly) e.value='';")
        return ua.create_action(ActionTypes.TYPE_TEXT, text=arg), f'type("{arg}") into {element_at_focus(env)}', None
    scroll = ActionTypes.SCROLL_DOWN_COORDS if arg == "down" else ActionTypes.SCROLL_UP_COORDS
    return ua.create_action(scroll, coords=np.array([W / 2, 130.0])), f'scroll("{arg}")', None


def element_at_focus(env):
    return env.unwrapped.instance.driver.execute_script(
        "var e=document.activeElement; return (e && e!==document.body) ? e.tagName.toLowerCase()+(e.id?'#'+e.id:'')+(e.readOnly?' (read-only, typing does nothing)':'') : 'nothing (click an input first)';"
    )


def mark(screenshot, pt):
    """Screenshot with a red dot where we clicked."""
    img = Image.fromarray(screenshot)
    if pt:
        ImageDraw.Draw(img).ellipse((pt[0] - 3, pt[1] - 3, pt[0] + 3, pt[1] + 3), outline="red", width=2)
    return np.asarray(img)


def frame(screenshot, caption):
    """Screenshot (3x) with a caption panel underneath."""
    img = Image.new("RGB", (W * SCALE, H * SCALE + 110), "black")
    img.paste(big(screenshot))
    ImageDraw.Draw(img).multiline_text((8, H * SCALE + 8), caption, font=FONT, fill="white", spacing=4)
    return img


def wrap(text, width=48):
    return "\n".join(textwrap.fill(line, width) for line in text.splitlines())


def save_video(frames, path):
    """frames -> GIF, plus MP4 if ffmpeg is around. Each frame shown 1.5s."""
    frames[0].save(f"{path}.gif", save_all=True, append_images=frames[1:], duration=1500, loop=0)
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{path}.gif", "-movflags", "faststart",
                        "-pix_fmt", "yuv420p", "-vf", "fps=10,scale=trunc(iw/2)*2:trunc(ih/2)*2", f"{path}.mp4"], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass


def contact_sheet(shots, cols=5):
    """All step screenshots in one numbered grid, so the VLM sees the whole episode in one image."""
    sheet = Image.new("RGB", (W * cols, H * ((len(shots) + cols - 1) // cols)), "white")
    for i, shot in enumerate(shots):
        x, y = W * (i % cols), H * (i // cols)
        sheet.paste(Image.fromarray(shot), (x, y))
        ImageDraw.Draw(sheet).text((x + 2, y + 2), str(i), font=FONT, fill="red")
    return sheet


def reflect(model, processor, task, reward, shots, history, lessons):
    """Review one episode's trace and return new lessons (Reflexion-style; no weight updates)."""
    outcome = "SUCCESS, the right flight was booked" if reward > 0 else (
        "FAILED, wrong flight booked" if reward < 0 and history and "Book" in history[-1] else "FAILED, ran out of steps")
    prompt = REFLECT_PROMPT.format(
        task=task, outcome=outcome,
        trace="\n".join(f"{i}: {h}" for i, h in enumerate(history)),
        known="\n".join(f"- {l}" for l in lessons) or "(none)",
    )
    new = [l.strip() for l in ask(model, processor, prompt, contact_sheet(shots), LESSONS_SCHEMA, 200)["lessons"] if len(l) > 10]
    # Keep lessons general: drop ones that leak this episode's values (quoted strings, dates, airport codes).
    specific = set(re.findall(r"\b[A-Z]{3}\b|\d+/\d+/\d+", task)) | set(re.findall(r"from: (.*?) to", task))
    new = [l for l in new if not any(s in l for s in specific) and not re.search(r"\d+/\d+|\(\d+,\s*\d+\)", l)]
    return [l for l in new if l not in lessons and l not in EXAMPLE_LESSONS][:3]


def run_episode(env, model, processor, seed, max_steps, log, video=False, temperature=0.0, lessons=None):
    env.reset(seed=seed)
    obs = prepare_episode(env)
    task = obs["utterance"]
    history, frames, shots, reward, done = [], [], [], -1.0, False
    print(f"\n=== seed {seed}: {task}")
    for t in range(max_steps):
        t0 = time.time()
        img = big(obs["screenshot"])
        prompt = PROMPT.format(
            task=task,
            history="\n".join(history[-8:]) or "(none)",
            lessons="".join(f"\nLesson from past attempts: {l}" for l in (lessons or [])[-10:]),
        )
        out = ask(model, processor, prompt, img, ACTION_SCHEMA, 150, temperature)
        raw = f"State: {out['state']}\nAction: {out['action']}(\"{out['arg']}\")"
        env_action, line, pt = execute(env, model, processor, (out["action"], out["arg"]), img)
        print(f"  {t:2d} [{time.time() - t0:.1f}s] {line}")
        shots.append(mark(obs["screenshot"], pt))
        if video:
            frames.append(frame(shots[-1], wrap(f"step {t}  ({time.time() - t0:.1f}s)\n{raw}\n=> {line}")[:450]))
        history.append(line)
        if env_action is None:
            continue
        obs, _, done, _, info = env.step(env_action)
        if done:
            reward = info["raw_reward"]
            break
    print(f"  => reward {reward}")
    if video:
        # on done, obs is blank, so reuse the last real screenshot
        frames.append(frame(shots[-1] if done else obs["screenshot"], f"DONE  reward = {reward}  ({'success' if reward > 0 else 'fail'})"))
        save_video(frames, f"runs/episode_seed{seed}")
        print(f"  video: runs/episode_seed{seed}.mp4")
    if lessons is not None:
        new = reflect(model, processor, task, reward, shots, history, lessons)
        lessons.extend(new)
        LESSONS_FILE.write_text(json.dumps(lessons, indent=1))
        for l in new:
            print(f"  + lesson: {l}")
    log.write(json.dumps({"seed": seed, "task": task, "reward": reward, "history": history}) + "\n")
    log.flush()
    return reward


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", type=int, default=5)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--show", action="store_true", help="show the Chrome window")
    p.add_argument("--video", action="store_true", help="save runs/episode_seed<N>.mp4/.gif with captioned steps")
    p.add_argument("--learn", action="store_true", help="reflect after each episode; lessons persist in runs/lessons.json")
    a = p.parse_args()

    model, processor = load(a.model)
    env = gym.make("miniwob/book-flight-v1", render_mode="human" if a.show else None, wait_ms=500)
    Path("runs").mkdir(exist_ok=True)
    lessons = (json.loads(LESSONS_FILE.read_text()) if LESSONS_FILE.exists() else []) if a.learn else None
    try:
        with open("runs/trajectories.jsonl", "a") as log:
            rewards = [run_episode(env, model, processor, a.seed + i, a.max_steps, log, a.video, a.temperature, lessons)
                       for i in range(a.episodes)]
    finally:
        env.close()
    wins = sum(r > 0 for r in rewards)
    print(f"\nsuccess {wins}/{len(rewards)} = {wins / len(rewards):.0%}")


if __name__ == "__main__":
    main()
