import { useAgentState } from "./api";
import AgentActivity from "./AgentActivity";
import DemoControls from "./DemoControls";
import IntentPanel from "./IntentPanel";
import Market from "./Market";
import Metrics from "./Metrics";
import Timeline from "./Timeline";
import { simClock } from "./format";

const STATUS: Record<string, { word: string; tone: string; line: string }> = {
  IDLE: { word: "Waiting for a trip", tone: "idle", line: "Nothing to own yet." },
  COMPILING: { word: "Reading", tone: "awake", line: "Liquid is compiling your request into durable intent." },
  DISCOVERING: { word: "Looking", tone: "awake", line: "Nimble is checking the live web for this route." },
  SLEEPING: { word: "Asleep", tone: "asleep", line: "Tinybird watches the market. Liquid is idle until a condition fires." },
  WAKING: { word: "Waking", tone: "awake", line: "A Tinybird condition fired. Loading the intent, not the chat." },
  REPLANNING: { word: "Replanning", tone: "alarm", line: "The world broke the plan. Finding a replacement." },
  BOOKING: { word: "Booking", tone: "awake", line: "Calling the booking provider." },
};

const NIMBLE_BADGE: Record<string, string> = {
  live: "live web",
  replay: "replaying a recorded live response",
  unavailable: "last call failed",
  "not called yet": "key set, not called yet",
  "NO API KEY": "missing API key",
};

export default function App() {
  const { state, offline } = useAgentState();
  if (!state) return <main className="loading">{offline ? "Backend unreachable on :8000. Start it with docker compose up." : "Connecting…"}</main>;
  const booked = state.agent_status === "SLEEPING" && state.intent?.status === "BOOKED";
  const st = booked
    ? { word: "Booked", tone: "asleep", line: "Agent asleep. Tinybird guards the booking; Liquid wakes only if the plan breaks." }
    : (STATUS[state.agent_status] ?? { word: state.agent_status, tone: "awake", line: "" });
  const waitingTokens = state.metrics.liquid_tokens_while_waiting;
  const integ = state.integrations;
  return (
    <main className={`app tone-${st.tone}`}>
      <header className="top">
        <div className="brand">
          <h1>HorizonBook</h1>
          <p className="tagline">An agent that owns the trip, not the chat.</p>
        </div>
        <div className="status" role="status" aria-live="polite">
          <span className="status-light" aria-hidden />
          <div>
            <span className="status-word">{st.word}</span>
            <span className="status-line">{st.line}</span>
          </div>
        </div>
        <div className="zero">
          <span className={`zero-value ${waitingTokens ? "nonzero" : ""}`}>{waitingTokens}</span>
          <span className="zero-label">Liquid tokens spent while waiting</span>
        </div>
        <ul className="integrations" aria-label="Integrations">
          <li className={integ.tinybird.mode === "live" ? "ok" : "bad"}>
            Tinybird {integ.tinybird.mode === "live" ? (integ.tinybird.host.includes("7181") ? "Local, live" : "Cloud, live") : integ.tinybird.mode.toLowerCase()}
          </li>
          <li className={integ.liquid.mode === "live" ? "ok" : "bad"}>
            Liquid {integ.liquid.model} {integ.liquid.mode === "live" ? "live" : "unreachable"}
          </li>
          <li className={integ.nimble.mode === "live" ? "ok" : integ.nimble.mode === "not called yet" ? "" : "bad"}>
            Nimble {NIMBLE_BADGE[integ.nimble.mode] ?? integ.nimble.mode}
          </li>
        </ul>
      </header>

      <IntentPanel state={state} />
      <Market flights={state.market} booking={state.booking} runKey={state.run_id ?? ""} />
      <AgentActivity state={state} />

      <footer className="bottom">
        <Metrics m={state.metrics} simTime={simClock(state.sim_clock)} />
        <Timeline events={state.events} />
        <DemoControls state={state} />
      </footer>
      {offline && <div className="offline">Lost the backend. Retrying…</div>}
    </main>
  );
}
