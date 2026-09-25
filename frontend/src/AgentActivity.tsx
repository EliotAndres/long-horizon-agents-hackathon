import type { HBEvent, State } from "./types";
import { clock, TOOL_LABEL } from "./format";

type Lane = { tool: "tinybird" | "nimble" | "liquid" | "sandbox"; role: string; types: string[] };

const LANES: Lane[] = [
  { tool: "tinybird", role: "Knows when the world matters", types: ["TRIGGER", "ARM", "CANDIDATES"] },
  { tool: "nimble", role: "Sees the current web", types: ["NIMBLE"] },
  { tool: "liquid", role: "Understands, decides, replans", types: ["LIQUID", "GUARDRAIL"] },
  { tool: "sandbox", role: "Books and cancels", types: ["BOOKED", "CANCELLED"] },
];

function laneText(e: HBEvent): string {
  const m = e.message;
  if (e.type === "TRIGGER") return m.startsWith("ACTIVE PLAN INVALID") ? "Active plan invalid: condition fired" : "Opportunity: condition fired";
  if (e.type === "ARM") return m.replace(" on Tinybird endpoints", "");
  if (e.type === "LIQUID") return m.split(" -- ")[0];
  if (e.type === "NIMBLE") return m.split(" [")[0];
  return m;
}

function detail(e: HBEvent): string | null {
  if (e.type === "TRIGGER") return e.message.split(": ").slice(1).join(": ");
  if (e.type === "LIQUID") {
    const p = e.payload as { total_tokens?: number; latency_ms?: number; context_bytes?: number; model?: string };
    const bits = [p.total_tokens && `${p.total_tokens} tokens`, p.latency_ms && `${(p.latency_ms / 1000).toFixed(1)} s`];
    if (p.context_bytes) bits.push(`${p.context_bytes} B of context, no chat history`);
    return bits.filter(Boolean).join(", ");
  }
  if (e.type === "NIMBLE") {
    const p = e.payload as { mode?: string; results?: { url: string }[] };
    const host = p.results?.[0]?.url ? new URL(p.results[0].url).hostname.replace("www.", "") : "";
    return [p.mode === "live" ? "live web" : p.mode, host && `top source ${host}`].filter(Boolean).join(", ");
  }
  return null;
}

export default function AgentActivity({ state }: { state: State }) {
  const events = state.events;
  const newest = events.length ? events[events.length - 1].id : 0;
  const log = [...events].reverse().filter((e) => e.type !== "RESET").slice(0, 40);
  return (
    <section className="panel activity" aria-label="Agent activity">
      <h2>What the agent did</h2>
      <ul className="lanes">
        {LANES.map((lane) => {
          const last = [...events].reverse().find((e) => e.source === lane.tool && lane.types.includes(e.type));
          const fresh = last && newest - last.id < 3 && state.agent_status !== "SLEEPING";
          const bad = last && (last.type === "GUARDRAIL" || last.message.includes("UNAVAILABLE"));
          return (
            <li key={lane.tool} className={`lane lane-${lane.tool} ${fresh ? "fresh" : ""}`}>
              <div className="lane-head">
                <span className="lane-name">{TOOL_LABEL[lane.tool]}</span>
                <span className="lane-role">{lane.role}</span>
              </div>
              {last ? (
                <div className={`lane-last ${bad ? "warn" : ""}`}>
                  <span className="tick" aria-hidden>{bad ? "!" : "✓"}</span>
                  <span>{laneText(last)}</span>
                </div>
              ) : (
                <div className="lane-last idle">Not needed yet</div>
              )}
              {last && detail(last) && <div className="lane-detail">{detail(last)}</div>}
            </li>
          );
        })}
      </ul>
      <ol className="log" aria-label="Event log">
        {log.map((e) => (
          <li key={e.id} className={`log-${e.source} log-type-${e.type.toLowerCase()}`}>
            <span className="log-time">{clock(e.timestamp)}</span>
            <span className="log-src">{TOOL_LABEL[e.source] ?? e.source}</span>
            <span className="log-msg">{e.message}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
