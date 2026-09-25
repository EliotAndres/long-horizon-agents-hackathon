import { useState } from "react";
import { post } from "./api";
import type { State } from "./types";

export default function DemoControls({ state }: { state: State }) {
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (key: string, path: string) => {
    setBusy(key);
    try {
      await post(path);
    } finally {
      setBusy(null);
    }
  };
  const asleep = state.agent_status === "SLEEPING" && state.world_phase === "live";
  const booked = state.booking?.status === "ACTIVE";
  return (
    <section className="controls" aria-label="World events">
      <p className="controls-note">
        The outside world. These buttons change fares and schedules, never the agent. Everything after is automatic.
      </p>
      <div className="controls-buttons">
        <button
          type="button"
          className="world"
          disabled={!state.intent || !!busy || !asleep || !!state.booking}
          onClick={() => run("sale", "/api/world/price-drop")}
        >
          {busy === "sale" ? "Publishing fares…" : "Fare sale: UA456 drops to $169"}
        </button>
        <button
          type="button"
          className="world"
          disabled={!booked || !!busy || !asleep}
          onClick={() => run("slip", "/api/world/schedule-change")}
        >
          {busy === "slip"
            ? "Publishing schedule…"
            : `Airline retimes ${state.booking?.candidate.flight_id ?? "the booked flight"}`}
        </button>
        <button type="button" className="reset" disabled={!!busy} onClick={() => run("reset", "/api/demo/reset")}>
          Reset demo
        </button>
      </div>
    </section>
  );
}
