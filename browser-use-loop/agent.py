"""VLM browser agent for MiniWoB++ book-flight, running Liquid AI LFM2.5-VL on-device via MLX.

Each step: screenshot + task + list of visible DOM elements -> VLM -> one action -> env.step.
Every episode is appended to runs/trajectories.jsonl (raw material for learning from its own habits).

    uv run agent.py --episodes 5            # headless
    uv run agent.py --episodes 1 --show     # watch Chrome
"""

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

import gymnasium as gym
import miniwob
from miniwob.action import ActionTypes
from mlx_vlm import apply_chat_template, generate, load
from PIL import Image, ImageDraw, ImageFont

gym.register_envs(miniwob)

MODEL = "LiquidAI/LFM2.5-VL-3B-MLX-8bit"
INPUTS = {"input_text", "textarea"}
CLICKABLE = {"button", "a", "li"}

PROMPT = """You control a web page to complete a task. The screenshot shows the page.

Task: {task}

Page (one line per row, [ID] marks elements you can act on):
{elements}

Your previous actions (already done, do not redo them):
{history}

Answer in exactly two lines:
State: <which fields are already filled, what popup is open>
Action: <ONE of click(ID) or type(ID, "text")>

Tips: typing in From/To opens a suggestion list; click the matching suggestion.
The date input is read-only (typing does nothing): click it to open a calendar, use Prev/Next to reach the right month, then click the day.
After searching, compare the flights and click the "Book flight" button of the right one."""


def visible_elements(obs):
    """Render the task area as text in DOM order, one line per visual row.
    Actionable elements get an [ID]; plain text is shown bare."""
    tags = {e["ref"]: e["tag"] for e in obs["dom_elements"]}
    lines, last_top = [], None
    for e in obs["dom_elements"]:
        if e["width"][0] <= 1 or e["height"][0] <= 1 or e["top"][0] >= 210:
            continue
        text = (e["text"] or e["value"]).strip()
        if e["tag"] in INPUTS:
            item = f'[{e["ref"]}] input #{e["id"]} value="{text}"'
        elif not text:
            continue
        elif e["tag"] in CLICKABLE or tags.get(e["parent"]) in CLICKABLE or e["id"].startswith("ui-id"):
            item = f'[{e["ref"]}] {e["tag"]} "{text}"'
        else:
            item = f'"{text}"'
        top = e["top"][0]
        if last_top is not None and abs(top - last_top) < 4:
            lines[-1] += "  " + item
        else:
            lines.append(item)
        last_top = top
    return "\n".join(lines)


def parse_action(text, obs):
    """First click(X) / type(X, "..") in the output; X is a ref number or an #html-id."""
    text = text.split("Action:")[-1]
    m = re.search(r'(click|type)\(\s*#?([\w-]+)\s*(?:,\s*["\'](.*?)["\'])?\s*\)', text)
    if not m:
        return None
    target = m[2]
    if not target.lstrip("-").isdigit():
        refs = [e["ref"] for e in obs["dom_elements"] if e["id"] == target]
        if not refs:
            return None
        target = refs[0]
    return (m[1], int(target), m[3] or "")


def to_env_action(env, action):
    kind, ref, text = action
    if kind == "click":
        return env.unwrapped.create_action(ActionTypes.CLICK_ELEMENT, ref=ref)
    # Clear the field first so retyping doesn't append.
    env.unwrapped.instance.driver.execute_script(
        "var e=core.previousDOMInfo[arguments[0]]; if(e&&e.value!==undefined) e.value='';", ref
    )
    return env.unwrapped.create_action(ActionTypes.FOCUS_ELEMENT_AND_TYPE_TEXT, ref=ref, text=text)


def prepare_episode(env):
    """Reset leaks state: MiniWoB doesn't reload the page, so the datepicker keeps focus and its
    calendar stays open (and red .error borders stay) into the next episode. Clear both, lift the timer, return a fresh obs."""
    # ponytail: book-flight has a 30s real-time limit; a local VLM needs ~1-3s/step, so we lift it
    # and score with raw_reward (+1/-1, no time decay). Remove once the agent is fast enough.
    env.unwrapped.instance.driver.execute_script(
        "clearTimeout(core.EP_TIMER); core.EPISODE_MAX_TIME=1e9;"
        "core.EP_TIMER=setTimeout(function(){core.endEpisode(-1,false,'timed out')},1e9);"
        "document.activeElement.blur(); $('.hasDatepicker').datepicker('hide'); $('.error').removeClass('error');"
    )
    return env.step(env.unwrapped.create_action(ActionTypes.NONE))[0]


FONT = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)


def frame(screenshot, caption):
    """Screenshot (3x) with a caption panel underneath."""
    shot = Image.fromarray(screenshot).resize((480, 630), Image.LANCZOS)
    img = Image.new("RGB", (480, 630 + 110), "black")
    img.paste(shot)
    ImageDraw.Draw(img).multiline_text((8, 638), caption, font=FONT, fill="white", spacing=4)
    return img


def save_video(frames, path):
    """frames -> GIF, plus MP4 if ffmpeg is around. Each frame shown 1.5s."""
    frames[0].save(f"{path}.gif", save_all=True, append_images=frames[1:], duration=1500, loop=0)
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{path}.gif", "-movflags", "faststart",
                        "-pix_fmt", "yuv420p", "-vf", "fps=10,scale=trunc(iw/2)*2:trunc(ih/2)*2", f"{path}.mp4"], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass


def wrap(text, width=48):
    import textwrap
    return "\n".join(textwrap.fill(line, width) for line in text.splitlines())


def act(model, processor, obs, history):
    prompt = PROMPT.format(
        task=obs["utterance"],
        elements=visible_elements(obs),
        history="\n".join(history[-6:]) or "(none)",
    )
    img = Image.fromarray(obs["screenshot"]).resize((480, 630), Image.LANCZOS)  # raw is 160x210
    chat = apply_chat_template(processor, model.config, prompt, num_images=1)
    out = generate(model, processor, chat, image=[img], max_tokens=100, temperature=0.0, verbose=False)
    return prompt, getattr(out, "text", out).strip()


def run_episode(env, model, processor, seed, max_steps, log, video=False):
    env.reset(seed=seed)
    obs = prepare_episode(env)
    history, steps, reward, frames, done = [], [], -1.0, [], False
    print(f"\n=== seed {seed}: {obs['utterance']}")
    for t in range(max_steps):
        t0 = time.time()
        prompt, raw = act(model, processor, obs, history)
        action = parse_action(raw, obs)
        print(f"  {t:2d} [{time.time() - t0:.1f}s] {raw!r}")
        steps.append({"prompt": prompt, "output": raw})
        if video:
            frames.append(frame(obs["screenshot"], wrap(f"step {t}  ({time.time() - t0:.1f}s)\n{raw}")[:400]))
        if action is None:
            history.append(f"{raw[:60]!r} -> INVALID, use click(ID) or type(ID, \"text\")")
            continue
        tag = next((e["tag"] for e in obs["dom_elements"] if e["ref"] == action[1]), None)
        if action[0] == "type" and tag not in INPUTS:
            history.append(f"{raw.split(chr(10))[-1]} -> FAILED, can only type into an input")
            continue
        if action[0] == "type" and env.unwrapped.instance.driver.execute_script(
            "return core.previousDOMInfo[arguments[0]].readOnly", action[1]
        ):
            history.append(f"{raw.split(chr(10))[-1]} -> FAILED, read-only: click it and pick from the popup")
            continue
        last_shot = obs["screenshot"]
        obs, _, done, _, info = env.step(to_env_action(env, action))
        history.append(f"{action[0]}({action[1]}" + (f', "{action[2]}")' if action[0] == "type" else ")"))
        if done:
            reward = info["raw_reward"]
            break
    print(f"  => reward {reward}")
    if video:
        # on done, obs is blank, so reuse the last real screenshot
        frames.append(frame(last_shot if done else obs["screenshot"], f"DONE  reward = {reward}  ({'success' if reward > 0 else 'fail'})"))
        save_video(frames, f"runs/episode_seed{seed}")
        print(f"  video: runs/episode_seed{seed}.mp4")
    log.write(json.dumps({"seed": seed, "task": steps and steps[0]["prompt"].split("\n")[2], "reward": reward, "steps": steps}) + "\n")
    log.flush()
    return reward


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", type=int, default=5)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--show", action="store_true", help="show the Chrome window")
    p.add_argument("--video", action="store_true", help="save runs/episode_seed<N>.mp4/.gif with captioned steps")
    a = p.parse_args()

    model, processor = load(a.model)
    env = gym.make("miniwob/book-flight-v1", render_mode="human" if a.show else None, wait_ms=500)
    Path("runs").mkdir(exist_ok=True)
    try:
        with open("runs/trajectories.jsonl", "a") as log:
            rewards = [run_episode(env, model, processor, a.seed + i, a.max_steps, log, a.video) for i in range(a.episodes)]
    finally:
        env.close()
    wins = sum(r > 0 for r in rewards)
    print(f"\nsuccess {wins}/{len(rewards)} = {wins / len(rewards):.0%}")


if __name__ == "__main__":
    main()
