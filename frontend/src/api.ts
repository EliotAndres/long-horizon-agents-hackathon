import { useEffect, useState } from "react";
import type { State } from "./types";

export async function post(path: string, body: unknown = {}): Promise<Response> {
  return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export function useAgentState(intervalMs = 600): { state: State | null; offline: boolean } {
  const [state, setState] = useState<State | null>(null);
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await fetch("/api/state");
        if (!r.ok) throw new Error(String(r.status));
        const s = (await r.json()) as State;
        if (alive) {
          setState(s);
          setOffline(false);
        }
      } catch {
        if (alive) setOffline(true);
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [intervalMs]);
  return { state, offline };
}
