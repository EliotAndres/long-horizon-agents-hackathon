import { useState } from "react";
import { post } from "./api";
import type { State } from "./types";
import { CONDITION_LABEL, longDate, money } from "./format";

function Handover({ state }: { state: State }) {
  const [text, setText] = useState(state.demo_request);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = sent || state.agent_status !== "IDLE";
  return (
    <form
      className="handover"
      onSubmit={async (e) => {
        e.preventDefault();
        setSent(true);
        setError(null);
        const r = await post("/api/intent", { text }).catch(() => null);
        if (!r?.ok) {
          setSent(false);
          setError(r ? `The backend refused the trip (${r.status}). Reset the demo and try again.` : "Backend unreachable.");
        }
      }}
    >
      <label htmlFor="request">Tell HorizonBook about the trip, once.</label>
      <textarea id="request" value={text} onChange={(e) => setText(e.target.value)} rows={7} disabled={busy} />
      <button type="submit" disabled={busy || !text.trim()}>
        {busy ? "Liquid is reading your request…" : "Hand over the trip"}
      </button>
      {error && <p className="form-error">{error}</p>}
    </form>
  );
}

export default function IntentPanel({ state }: { state: State }) {
  const { intent, booking, conditions } = state;
  if (!intent) {
    return (
      <section className="panel intent" aria-label="Trip">
        <h2>The trip</h2>
        <Handover key={state.run_id ?? ""} state={state} />
      </section>
    );
  }
  const hc = intent.hard_constraints;
  const pref = intent.preferences;
  return (
    <section className="panel intent" aria-label="Trip">
      <h2>The trip it owns</h2>
      <div className="route">
        <span className="route-codes">
          {hc.allowed_origins.join(" / ")} <span className="route-arrow">→</span> {intent.destination}
        </span>
        <span className="route-date">{longDate(intent.travel_date)}</span>
      </div>
      <dl className="rules">
        <dt>Must</dt>
        <dd>
          <span>Arrive by {hc.arrival_before}</span>
          <span>Leave after {hc.departure_after}</span>
          <span>Pay at most {money(hc.max_price)}</span>
        </dd>
        <dt>Prefers</dt>
        <dd>
          {pref.prefer_nonstop && <span>Nonstop</span>}
          <span>{pref.preferred_origins.join(", ")} first</span>
        </dd>
        <dt>May</dt>
        <dd>
          <span>{intent.booking_policy.auto_book ? "Book without asking" : "Ask before booking"}</span>
        </dd>
      </dl>
      <p className="provenance">
        Compiled by {intent.compiled_by.replace("liquid:", "Liquid ")}. Stored as durable state; no chat kept.
      </p>

      <h3>Current plan</h3>
      {booking ? (
        <div className={`plan plan-${booking.status.toLowerCase()}`}>
          <div className="plan-head">
            <span className="plan-flight">{booking.candidate.flight_id}</span>
            <span className="plan-id">{booking.booking_id}</span>
            <span className="plan-status">{booking.status === "ACTIVE" ? "Confirmed" : "Invalid"}</span>
          </div>
          <div className="plan-times">
            {booking.candidate.origin} {booking.candidate.departure} → {booking.candidate.destination}{" "}
            {booking.candidate.arrival}<span className="plan-price">{money(booking.candidate.price)}</span>
          </div>
          {booking.reasoning && <p className="plan-why">“{booking.reasoning}”</p>}
          <ul className="plan-just">
            {booking.justified_by.map((j) => (
              <li key={j}>{j}</li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="plan-none">Nothing booked. No flight satisfies every rule yet.</p>
      )}

      <h3>Wakes the agent when</h3>
      <ul className="conditions">
        {conditions.map((c) => (
          <li key={c.id}>
            <span className="cond-type">{CONDITION_LABEL[c.type] ?? c.type}</span>
            <span className="cond-rule">{c.rule}</span>
            <span className="cond-by">{c.created_by.startsWith("liquid") ? "set by Liquid" : c.created_by === "policy-floor" ? "safety floor" : "policy"}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
