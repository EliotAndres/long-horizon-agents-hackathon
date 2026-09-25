import type { Metrics as M } from "./types";

export default function Metrics({ m, simTime }: { m: M; simTime: string }) {
  const items: [string, string | number, string?][] = [
    ["Market observations", m.market_observations.toLocaleString(), "streamed into Tinybird"],
    ["Price changes slept through", m.market_changes.toLocaleString()],
    ["Tinybird condition checks", m.tinybird_checks.toLocaleString(), `${m.tinybird_fires} fired`],
    ["Nimble web lookups", m.nimble_calls, `${m.nimble_live} live`],
    ["Liquid calls", m.liquid_calls, `1 intent + ${m.wakes} wake${m.wakes === 1 ? "" : "s"}, ${m.liquid_tokens.toLocaleString()} tokens`],
    ["Sandbox bookings", m.bookings, `${m.cancellations} cancelled`],
  ];
  return (
    <section className="metrics" aria-label="Metrics">
      {items.map(([label, value, sub]) => (
        <div className="metric" key={label}>
          <span className="metric-value">{value}</span>
          <span className="metric-label">{label}</span>
          {sub && <span className="metric-sub">{sub}</span>}
        </div>
      ))}
      <div className="metric metric-clock">
        <span className="metric-value">{simTime || "—"}</span>
        <span className="metric-label">Simulated market time</span>
      </div>
    </section>
  );
}
