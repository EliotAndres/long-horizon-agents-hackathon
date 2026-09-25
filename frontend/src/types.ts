export type Flight = {
  flight_id: string;
  carrier: string;
  origin: string;
  destination: string;
  departure: string;
  arrival: string;
  price: number;
  stops: number;
  available: boolean;
  violations?: string[];
  qualifies?: boolean;
  booked?: boolean;
};

export type Intent = {
  id: string;
  request_text: string;
  origin: string;
  destination: string;
  travel_date: string;
  hard_constraints: { arrival_before: string; departure_after: string; max_price: number; allowed_origins: string[] };
  preferences: { prefer_nonstop: boolean; preferred_origins: string[] };
  booking_policy: { auto_book: boolean };
  status: string;
  compiled_by: string;
};

export type Booking = {
  booking_id: string;
  candidate: Flight;
  created_at: string;
  status: "ACTIVE" | "INVALID" | "CANCELLED";
  justified_by: string[];
  reasoning: string;
  decided_by: string;
};

export type Condition = { id: string; type: string; rule: string; created_by: string; armed: boolean };

export type HBEvent = {
  id: number;
  timestamp: string;
  type: string;
  source: string;
  message: string;
  payload: Record<string, unknown>;
};

export type Metrics = Record<
  | "market_observations"
  | "market_changes"
  | "tinybird_checks"
  | "tinybird_fires"
  | "nimble_calls"
  | "nimble_live"
  | "liquid_calls"
  | "liquid_tokens"
  | "liquid_tokens_while_waiting"
  | "wakes"
  | "bookings"
  | "cancellations",
  number
>;

export type State = {
  agent_status: string;
  run_id: string | null;
  intent: Intent | null;
  booking: Booking | null;
  bookings: Booking[];
  conditions: Condition[];
  market: Flight[];
  events: HBEvent[];
  metrics: Metrics;
  sleep_started_at: string | null;
  sim_clock: string | null;
  world_phase: string;
  integrations: {
    tinybird: { mode: string; host: string };
    liquid: { mode: string; model: string | null };
    nimble: { mode: string };
  };
  demo_request: string;
};
