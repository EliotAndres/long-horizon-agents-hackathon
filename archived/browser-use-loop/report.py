"""Build a self-contained HTML page of every step (screenshot + state + action + result) for one run.

    uv run report.py                 # latest run -> runs/report.html
    uv run report.py --run 20260925-122204

ponytail: frames are saved per seed (runs/frames/seed<N>_<t>.png), so only the latest run of a seed has
the right images. Add the run id to the frame path if older runs need replaying.
"""

import argparse
import base64
import json
from pathlib import Path

RUNS = Path("runs")

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Book-flight Step Replay</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root {
  --paper: #F3F5F8; --surface: #FFFFFF; --ink: #18202B; --muted: #5B6675; --rule: #DCE2E9;
  --accent: #1F4E9C; --banner: #F7D64A; --banner-ink: #1A1A12; --bad: #B9382F; --good: #2F7D4F; --warn: #9A6412;
  --warn-bg: #FBEFD9;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --paper: #10151C; --surface: #17202B; --ink: #E6EBF1; --muted: #9AA6B5; --rule: #2A3542;
    --accent: #8DB0EC; --banner: #E3C23E; --banner-ink: #16140A; --bad: #F08A80; --good: #7CCB9A; --warn: #E7B45E;
    --warn-bg: #3A2C14;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --paper: #10151C; --surface: #17202B; --ink: #E6EBF1; --muted: #9AA6B5; --rule: #2A3542;
  --accent: #8DB0EC; --banner: #E3C23E; --banner-ink: #16140A; --bad: #F08A80; --good: #7CCB9A; --warn: #E7B45E;
  --warn-bg: #3A2C14;
}
body { background: var(--paper); color: var(--ink); font: 15px/1.5 "IBM Plex Sans", system-ui, sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; padding-inline: 20px; padding-block: 28px 64px; display: grid; gap: 24px; }
.mono { font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace; }
header { display: grid; gap: 14px; }
.eyebrow { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
h1 { margin: 0; font-size: 28px; font-weight: 600; text-wrap: balance; }
.stats { display: flex; flex-wrap: wrap; gap: 8px 28px; font-variant-numeric: tabular-nums; }
.stat b { display: block; font-size: 22px; font-weight: 600; }
.stat span { font-size: 13px; color: var(--muted); }
.tabs { display: flex; flex-wrap: wrap; gap: 8px; }
.tab { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border: 1px solid var(--rule); border-radius: 999px;
  background: var(--surface); color: var(--ink); font: inherit; font-size: 14px; cursor: pointer; }
.tab[aria-selected="true"] { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.tab:focus-visible, .step:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--bad); }
.dot.ok { background: var(--good); }
.task { background: var(--banner); color: var(--banner-ink); padding: 12px 16px; border-radius: 4px; font-size: 16px; }
.task small { display: block; font-size: 13px; opacity: .75; }
.lessons { display: grid; gap: 6px; font-size: 14px; }
.lessons p { margin: 0; }
.lessons .add::before { content: "+ "; color: var(--good); font-weight: 600; }
.lessons .rm { color: var(--muted); text-decoration: line-through; }
.lessons .rm::before { content: "− "; color: var(--bad); font-weight: 600; text-decoration: none; display: inline-block; }
.steps { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 16px; }
.step { background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; display: grid; grid-template-rows: auto 1fr; }
.step img { width: 100%; aspect-ratio: 160 / 210; display: block; background: var(--rule); }
.body { padding: 10px 12px 12px; display: grid; gap: 6px; align-content: start; }
.head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.n { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.act { font-size: 13px; font-weight: 500; color: var(--accent); overflow-wrap: anywhere; }
.state { margin: 0; font-size: 13.5px; }
.result { margin: 0; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.flag { justify-self: start; font-size: 11.5px; letter-spacing: .04em; text-transform: uppercase; padding: 2px 8px;
  border-radius: 3px; color: var(--warn); background: var(--warn-bg); }
.memory { border-top: 1px solid var(--rule); padding-top: 16px; display: grid; gap: 8px; }
.memory h2 { margin: 0; font-size: 16px; font-weight: 600; }
.memory ol { margin: 0; padding-left: 20px; display: grid; gap: 4px; font-size: 14px; }
.empty { color: var(--muted); font-size: 14px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow mono">MiniWoB++ book-flight · LFM2.5-VL-3B on-device · run <span id="run"></span></div>
    <h1>Step-by-step replay</h1>
    <div class="stats" id="stats"></div>
  </header>
  <nav class="tabs" role="tablist" id="tabs" aria-label="Episodes"></nav>
  <section id="episode" style="display:grid;gap:16px"></section>
  <section class="memory">
    <h2>Lessons in memory after this run</h2>
    <ol id="memory"></ol>
  </section>
</div>

<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

$("run").textContent = DATA.run;
const eps = DATA.episodes, allSteps = eps.flatMap(e => e.steps);
const stat = (v, l) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`;
$("stats").innerHTML =
  stat(`${eps.filter(e => e.reward > 0).length}/${eps.length}`, "episodes booked correctly") +
  stat(allSteps.length, "steps taken") +
  stat(`${Math.round(100 * allSteps.filter(s => !s.changed).length / Math.max(1, allSteps.length))}%`, "steps that changed nothing") +
  stat(DATA.memory.length, "lessons stored");
$("memory").innerHTML = DATA.memory.length ? DATA.memory.map(l => `<li>${esc(l)}</li>`).join("") : '<li class="empty">None</li>';

$("tabs").innerHTML = eps.map((e, i) =>
  `<button class="tab" role="tab" id="tab${i}" aria-selected="false" data-i="${i}">
     <span class="dot ${e.reward > 0 ? "ok" : ""}" aria-hidden="true"></span>Seed ${e.seed}
     <span class="mono" style="color:var(--muted);font-size:12px">${e.steps.length} steps</span></button>`).join("");

function show(i) {
  const e = eps[i];
  document.querySelectorAll(".tab").forEach(t => t.setAttribute("aria-selected", t.dataset.i == i));
  const lessons = [...e.lessons_removed.map(l => `<p class="rm">${esc(l)}</p>`), ...e.lessons_added.map(l => `<p class="add">${esc(l)}</p>`)];
  $("episode").innerHTML = `
    <div class="task">${esc(e.task)}<small>${e.reward > 0 ? "Booked correctly" : "Failed"} · reward ${e.reward}</small></div>
    ${lessons.length ? `<div class="lessons"><div class="eyebrow">What reflection changed after this episode</div>${lessons.join("")}</div>` : ""}
    <div class="steps">${e.steps.map((s, t) => `
      <article class="step" tabindex="0">
        <img src="${s.img}" alt="Screen before step ${t}" loading="lazy">
        <div class="body">
          <div class="head"><span class="act mono">${esc(s.action)}</span><span class="n">step ${t}</span></div>
          <p class="state">${esc(s.state)}</p>
          <p class="result mono">${esc(s.result.replace(/^.*?-> /, "→ ").replace(/^(type|scroll)\(.*?\)( into)?/, m => m.startsWith("type") ? "→ typed into" : "→ scrolled"))}</p>
          ${s.changed ? "" : '<span class="flag">Screen did not change</span>'}
        </div>
      </article>`).join("")}</div>`;
  try { history.replaceState(null, "", "#seed" + e.seed); } catch (_) {}
}
$("tabs").addEventListener("click", ev => { const b = ev.target.closest(".tab"); if (b) show(+b.dataset.i); });
const fromHash = eps.findIndex(e => "#seed" + e.seed === location.hash);
show(fromHash >= 0 ? fromHash : 0);
</script>
</body>
</html>
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", help="run id (default: latest)")
    p.add_argument("--out", default=str(RUNS / "report.html"))
    a = p.parse_args()

    records = [json.loads(l) for l in (RUNS / "trajectories.jsonl").read_text().splitlines() if '"run"' in l]
    run = a.run or records[-1]["run"]
    episodes = [r for r in records if r["run"] == run]
    for e in episodes:
        for t, s in enumerate(e["steps"]):
            png = (RUNS / "frames" / f"seed{e['seed']}_{t:02d}.png").read_bytes()
            s["img"] = "data:image/png;base64," + base64.b64encode(png).decode()
    memory = json.loads((RUNS / "lessons.json").read_text()) if (RUNS / "lessons.json").exists() else []
    data = json.dumps({"run": run, "episodes": episodes, "memory": memory}).replace("</", "<\\/")
    Path(a.out).write_text(PAGE.replace("__DATA__", data))
    print(f"{a.out}: {len(episodes)} episodes, {sum(len(e['steps']) for e in episodes)} steps")


if __name__ == "__main__":
    main()
