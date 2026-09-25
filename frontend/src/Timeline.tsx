import type { HBEvent } from "./types";
import { clock } from "./format";

type Step = { label: string; match: (e: HBEvent) => boolean };

const STEPS: Step[] = [
  { label: "User intent", match: (e) => e.type === "USER_INTENT" },
  { label: "Sleep", match: (e) => e.type === "SLEEP" },
  { label: "Price drop", match: (e) => e.type === "WORLD" && e.message.includes("fare sale") },
  { label: "Wake", match: (e) => e.type === "WAKE" },
  { label: "Book", match: (e) => e.type === "BOOKED" },
  { label: "Sleep", match: (e) => e.type === "SLEEP" },
  { label: "Schedule change", match: (e) => e.type === "WORLD" && e.message.includes("retimed") },
  { label: "Wake", match: (e) => e.type === "WAKE" },
  { label: "Rebook", match: (e) => e.type === "REBOOKED" },
  { label: "Sleep", match: (e) => e.type === "SLEEP" },
];

export default function Timeline({ events }: { events: HBEvent[] }) {
  const reached: (string | null)[] = STEPS.map(() => null);
  let i = 0;
  for (const e of events) {
    if (i < STEPS.length && STEPS[i].match(e)) {
      reached[i] = e.timestamp;
      i += 1;
    }
  }
  return (
    <ol className="timeline" aria-label="Timeline">
      {STEPS.map((s, idx) => (
        <li
          key={idx}
          className={`step ${reached[idx] ? "done" : ""} ${idx === i ? "next" : ""} ${s.label === "Sleep" ? "sleep" : ""}`}
        >
          <span className="step-dot" />
          <span className="step-label">{s.label}</span>
          <span className="step-time">{reached[idx] ? clock(reached[idx]) : ""}</span>
        </li>
      ))}
    </ol>
  );
}
