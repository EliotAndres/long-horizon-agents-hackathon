export const money = (n: number) => `$${Math.round(n)}`;

export function clock(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function simClock(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("en-US", {
    weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC",
  });
}

export function longDate(ymd: string): string {
  const d = new Date(`${ymd}T12:00:00Z`);
  return d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: "UTC" });
}

const REASONS: Record<string, string> = {
  PRICE: "price",
  "ARRIVES LATE": "arrives late",
  "DEPARTS EARLY": "departs early",
  "SOLD OUT": "sold out",
  ROUTE: "wrong airport",
};
export const reason = (v: string) => REASONS[v] ?? v.toLowerCase();

export const TOOL_LABEL: Record<string, string> = {
  tinybird: "Tinybird",
  nimble: "Nimble",
  liquid: "Liquid",
  sandbox: "Sandbox",
  agent: "Agent",
  world: "World",
  user: "You",
};

export const CONDITION_LABEL: Record<string, string> = {
  price_below: "A flight meets every rule",
  better_candidate: "A much cheaper flight appears",
  arrival_after: "Booked flight would land late",
  availability_changed: "Booked flight disappears",
};
