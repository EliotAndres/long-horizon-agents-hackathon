"""Read the agent's traces (runs/trajectories.jsonl) and derive learnings across ALL episodes.

agent.py --learn reflects on one episode at a time; this looks at every episode together and finds:
  - playbook:  the steps successful episodes take, in order (what to click / type, where)
  - wasted:    targets whose clicks usually do nothing or can't be found
  - mistakes:  typing with no input focused, typing into read-only fields, repeating a dead action...
  - lessons:   "If <situation> then <do>" rules backed by counts, in the same form agent.py uses
  - scores:    for lessons agent.py learned, success rate while the lesson was active vs not

    uv run learn.py                      # report + runs/learnings.json
    uv run learn.py --apply              # also merge new lessons into runs/lessons.json, drop harmful ones
    uv run learn.py --llm                # also let Liquid LFM2.5 condense the evidence into lessons
    uv run learn.py --run 20260925-122204
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

RUNS = Path("runs")
MODEL = "LiquidAI/LFM2.5-VL-3B-MLX-8bit"

STEP = re.compile(r'^(click|type|scroll)\("(.*?)"\)')
ELEMENT = re.compile(r" on (\w+(?:#[\w-]+)?)")
TYPED_INTO = re.compile(r" into (\w+(?:#[\w-]+)?)")


def load(path, run=None):
    episodes = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [e for e in episodes if run in (None, e["run"])]


def parse_step(step):
    """One logged step -> {kind, arg, target, effective, problem}. target is the page element (tag#id) when known."""
    action, result = step["action"], step["result"]
    m = STEP.match(action)
    kind, arg = (m.group(1), m.group(2)) if m else ("other", action)
    el = ELEMENT.search(result) or (TYPED_INTO.search(result) if kind == "type" else None)
    target = el.group(1) if el else None
    problem = None
    if "FAILED, could not find" in result:
        problem, target = "not_found", None
    elif "nothing (click an input first)" in result:
        problem, target = "type_without_focus", None
    elif "read-only" in result:
        problem = "type_read_only"
    elif not step.get("changed", True) or "did NOT change" in result:
        problem = "no_effect"
    key = f"scroll:{arg}" if kind == "scroll" else f"{kind}:{target or '?'}"
    return {"kind": kind, "arg": arg, "target": target, "key": key, "effective": problem is None, "problem": problem}


def specifics(episodes):
    """Values that belong to one task (cities, codes, dates): lessons and labels must not contain them."""
    words = set()
    for e in episodes:
        words |= set(re.findall(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b|\b[A-Z]{3}\b|\d+/\d+/\d+", e["task"]))
    return words - {"Book", "Search", "From", "To", "Leave", "Depart", "Return"}


def is_general(text, specific):
    return not re.search(r"\d", text) and not any(s in text for s in specific)


def generalize(text, specific):
    """'Boston suggestion' -> '<value> suggestion', 'day 12' -> 'day <n>'."""
    for s in sorted(specific, key=len, reverse=True):
        text = text.replace(s, "<value>")
    return re.sub(r"\$?\d[\d,./:]*", "<n>", text)


def playbook(successes, specific, min_share=0.5):
    """Steps that most successful episodes take, ordered by where they usually happen.
    The 2nd click on the same kind of element is its own step ("click:div#2": e.g. the To suggestion)."""
    positions, labels, seen_in = defaultdict(list), defaultdict(Counter), Counter()
    for e in successes:
        keys, times = [], Counter()
        for s in map(parse_step, e["steps"]):
            if s["effective"] and s["key"] != "click:?" and (not keys or keys[-1][2] != s["key"]):
                times[s["key"]] += 1
                n = times[s["key"]]
                keys.append((s["key"] + (f"#{n}" if n > 1 else ""), s["arg"], s["key"]))
        keys = [(k, a) for k, a, _ in keys]
        for i, (key, arg) in enumerate(keys):
            positions[key].append(i / max(len(keys) - 1, 1))
            labels[key][arg] += 1
        seen_in.update({k for k, _ in keys})
    steps = []
    for key, n in seen_in.items():
        if n / len(successes) < min_share:
            continue
        general = [a for a, _ in labels[key].most_common() if is_general(a, specific)]
        kind, element = key.split(":", 1)
        label = general[0] if general else ("the task's value" if kind == "type" else
                                            generalize(labels[key].most_common(1)[0][0], specific))
        steps.append({"step": f'{kind}("{label}")', "element": re.sub(r"#\d+$", "", element),
                      "share": round(n / len(successes), 2), "at": sum(positions[key]) / len(positions[key])})
    return [{k: v for k, v in s.items() if k != "at"} for s in sorted(steps, key=lambda s: s["at"])]


def wasted_targets(episodes, min_count):
    tries, wasted = Counter(), Counter()
    for e in episodes:
        for s in map(parse_step, e["steps"]):
            if s["kind"] == "click":
                name = s["target"] or s["arg"].lower()
                tries[name] += 1
                wasted[name] += not s["effective"]
    return [{"target": t, "clicks": tries[t], "wasted": w, "rate": round(w / tries[t], 2)}
            for t, w in wasted.most_common() if w >= min_count and w / tries[t] >= 0.5]


def mistakes(episodes):
    """Counts of each mistake, how many episodes had it, and the success rate of those episodes."""
    out = {}
    per_episode = [(e["reward"] > 0, [parse_step(s) for s in e["steps"]]) for e in episodes]
    checks = {
        "type_without_focus": lambda st: [s for s in st if s["problem"] == "type_without_focus"],
        "type_read_only": lambda st: [s for s in st if s["problem"] == "type_read_only"],
        "not_found": lambda st: [s for s in st if s["problem"] == "not_found"],
        "repeated_dead_action": lambda st: [b for a, b in zip(st, st[1:])
                                            if a["problem"] and a["kind"] == b["kind"] and a["arg"] == b["arg"]],
        "scroll_after_not_found_helped": lambda st: [c for a, b, c in zip(st, st[1:], st[2:])
                                                     if a["problem"] == "not_found" and b["kind"] == "scroll"
                                                     and c["kind"] == "click" and c["effective"]],
    }
    for name, find in checks.items():
        hits = [(ok, len(find(st))) for ok, st in per_episode]
        eps = [ok for ok, n in hits if n]
        out[name] = {"count": sum(n for _, n in hits), "episodes": len(eps),
                     "success_rate": round(sum(eps) / len(eps), 2) if eps else None}
    return out


RULES = {
    "type_without_focus": "If no input field is focused, click the input first, because typing does nothing otherwise.",
    "type_read_only": "If a field is read-only, click it to open its picker instead of typing into it.",
    "repeated_dead_action": "If an action did not change the screen, do not repeat it; try a different element or scroll.",
    "scroll_after_not_found_helped": "If you cannot find what to click, scroll to reveal it before trying again.",
}


def derive_lessons(found, n_episodes, min_count):
    lessons = []
    for name, text in RULES.items():
        m = found[name]
        if m["count"] >= min_count:
            lessons.append({"lesson": text, "from": name,
                            "evidence": f'{m["count"]}x in {m["episodes"]}/{n_episodes} episodes'
                                        + (f', those episodes succeeded {m["success_rate"]:.0%}' if m["success_rate"] is not None else "")})
    return lessons


def score_lessons(episodes, min_n=3):
    """Success rate of episodes while each learned lesson was in the prompt vs while it wasn't."""
    active, windows = set(), defaultdict(list)
    all_lessons = {l for e in episodes for l in e.get("lessons_added", [])}
    for e in episodes:
        for l in all_lessons:
            windows[(l, l in active)].append(e["reward"] > 0)
        active |= set(e.get("lessons_added", []))
        active -= set(e.get("lessons_removed", []))
    scores = []
    for l in all_lessons:
        on, off = windows[(l, True)], windows[(l, False)]
        if len(on) < min_n or not off:
            verdict, delta = "unclear", None
        else:
            delta = sum(on) / len(on) - sum(off) / len(off)
            verdict = "helpful" if delta >= 0.15 else "harmful" if delta <= -0.15 else "neutral"
        scores.append({"lesson": l, "episodes_with": len(on), "verdict": verdict,
                       "success_with": round(sum(on) / len(on), 2) if on else None,
                       "success_without": round(sum(off) / len(off), 2) if off else None,
                       "delta": None if delta is None else round(delta, 2)})
    return sorted(scores, key=lambda s: -(s["delta"] or 0))


def llm_lessons(report, model_name):
    """Let the on-device Liquid model turn the evidence into at most 3 general lessons (text only)."""
    from mlx_vlm import apply_chat_template, generate, load as load_model
    from mlx_vlm.structured import build_json_schema_logits_processor
    model, processor = load_model(model_name)
    prompt = ("You coach a web agent. Below is evidence mined from all its past attempts on one website.\n"
              f"{json.dumps(report, indent=1)[:6000]}\n\nWrite 1 to 3 general lessons of the form "
              '"If <situation you see> then <what to do>". No specific cities, codes, dates or prices. '
              'Answer in JSON: {"lessons": [...]}')
    schema = {"type": "object", "required": ["lessons"], "properties": {
        "lessons": {"type": "array", "items": {"type": "string", "maxLength": 160}, "minItems": 1, "maxItems": 3}}}
    chat = apply_chat_template(processor, model.config, prompt, num_images=0)
    out = generate(model, processor, chat, max_tokens=250, temperature=0.0, verbose=False,
                   logits_processors=[build_json_schema_logits_processor(processor.tokenizer, schema)])
    return json.loads(getattr(out, "text", out))["lessons"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=Path, default=RUNS)
    p.add_argument("--run", help="only this run id (default: all runs)")
    p.add_argument("--min-count", type=int, default=2, help="evidence needed before something becomes a lesson")
    p.add_argument("--apply", action="store_true", help="merge lessons into runs/lessons.json, drop harmful ones")
    p.add_argument("--llm", action="store_true", help="also ask Liquid LFM2.5 to condense the evidence into lessons")
    p.add_argument("--model", default=MODEL)
    a = p.parse_args()

    episodes = load(a.runs / "trajectories.jsonl", a.run)
    if not episodes:
        raise SystemExit(f"no episodes in {a.runs / 'trajectories.jsonl'}" + (f" for run {a.run}" if a.run else ""))
    successes = [e for e in episodes if e["reward"] > 0]
    specific = specifics(episodes)
    by_run = defaultdict(list)
    for e in episodes:
        by_run[e["run"]].append(e["reward"] > 0)

    found = mistakes(episodes)
    report = {
        "episodes": len(episodes),
        "success_rate": round(len(successes) / len(episodes), 2),
        "avg_steps": {"success": round(sum(len(e["steps"]) for e in successes) / len(successes), 1) if successes else None,
                      "fail": round(sum(len(e["steps"]) for e in episodes if e["reward"] <= 0) / max(len(episodes) - len(successes), 1), 1)},
        "runs": {r: f"{sum(v)}/{len(v)}" for r, v in by_run.items()},
        "playbook": playbook(successes, specific) if successes else [],
        "wasted_clicks": wasted_targets(episodes, a.min_count),
        "mistakes": found,
        "lessons": derive_lessons(found, len(episodes), a.min_count),
        "lesson_scores": score_lessons(episodes),
    }
    if a.llm:
        report["llm_lessons"] = [l for l in llm_lessons(report, a.model) if is_general(l, specific)]
    (a.runs / "learnings.json").write_text(json.dumps(report, indent=1))

    print(f"{report['episodes']} episodes, success {report['success_rate']:.0%}  "
          f"(avg steps: {report['avg_steps']['success']} when it works, {report['avg_steps']['fail']} when it fails)")
    print("runs:", ", ".join(f"{r} {v}" for r, v in report["runs"].items()))
    if report["playbook"]:
        print("\nplaybook (what successful episodes do, in order):")
        for i, s in enumerate(report["playbook"], 1):
            print(f"  {i}. {s['step']:40} on {s['element']:20} in {s['share']:.0%} of successes")
    if report["wasted_clicks"]:
        print("\nclicks that usually fail (screen didn't change, or target not found):")
        for w in report["wasted_clicks"]:
            print(f"  {w['target']:30} {w['wasted']}/{w['clicks']} wasted")
    print("\nlessons from the evidence:")
    for l in report["lessons"] + [{"lesson": l, "evidence": "Liquid LFM2.5"} for l in report.get("llm_lessons", [])]:
        print(f"  + {l['lesson']}  [{l['evidence']}]")
    for s in report["lesson_scores"]:
        if s["verdict"] != "unclear":
            print(f"  {s['verdict']:8} {s['success_with']:.0%} with vs {s['success_without']:.0%} without: {s['lesson']}")
    print(f"\nwrote {a.runs / 'learnings.json'}")

    if a.apply:
        path = a.runs / "lessons.json"
        current = json.loads(path.read_text()) if path.exists() else []
        harmful = {s["lesson"] for s in report["lesson_scores"] if s["verdict"] == "harmful"}
        added = [l for l in [l["lesson"] for l in report["lessons"]] + report.get("llm_lessons", []) if l not in current]
        dropped = [l for l in current if l in harmful]
        path.write_text(json.dumps([l for l in current if l not in harmful] + added, indent=1))
        print(f"{path}: +{len(added)} new, -{len(dropped)} harmful")


if __name__ == "__main__":
    main()
