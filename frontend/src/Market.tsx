import { useEffect, useRef } from "react";
import type { Booking, Flight } from "./types";
import { money, reason } from "./format";

type Baseline = Record<string, { price: number; arrival: string }>;

export default function Market({ flights, booking, runKey }: { flights: Flight[]; booking: Booking | null; runKey: string }) {
  const base = useRef<{ key: string; b: Baseline }>({ key: "", b: {} });
  if (base.current.key !== runKey) base.current = { key: runKey, b: {} };
  for (const f of flights) base.current.b[f.flight_id] ??= { price: f.price, arrival: f.arrival };

  const prev = useRef<Record<string, number>>({});
  const moved = new Set(flights.filter((f) => prev.current[f.flight_id] !== undefined && prev.current[f.flight_id] !== f.price).map((f) => f.flight_id));
  useEffect(() => {
    prev.current = Object.fromEntries(flights.map((f) => [f.flight_id, f.price]));
  });

  const rows = [...flights].sort((a, b) => a.departure.localeCompare(b.departure));
  const qualifying = flights.filter((f) => f.qualifies).length;
  return (
    <section className="panel market" aria-label="Market">
      <div className="panel-head">
        <h2>Live market</h2>
        <span className="market-summary">
          {qualifying === 1 ? "1 flight meets every rule" : qualifying ? `${qualifying} flights meet every rule` : "No flight meets every rule"}
        </span>
      </div>
      <table>
        <thead>
          <tr>
            <th scope="col">Flight</th>
            <th scope="col">Times</th>
            <th scope="col">Stops</th>
            <th scope="col" className="num">Fare</th>
            <th scope="col">Against your rules</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((f) => {
            const b = base.current.b[f.flight_id];
            const dropped = b && f.qualifies && b.price - f.price >= 15;
            const retimed = b && b.arrival !== f.arrival;
            const isBooked = booking && booking.status !== "CANCELLED" && booking.candidate.flight_id === f.flight_id;
            const bookedInvalid = isBooked && (booking!.status === "INVALID" || !f.qualifies);
            const cls = isBooked ? (bookedInvalid ? "row-broken" : "row-booked") : f.qualifies ? "row-ok" : "row-no";
            return (
              <tr key={f.flight_id} className={cls}>
                <td>
                  <span className="fid">{f.flight_id}</span>
                  <span className="carrier">
                    {f.carrier}, {f.origin} → {f.destination}
                  </span>
                </td>
                <td className="times">
                  {f.departure} → {retimed ? <><s>{b.arrival}</s> <b className="late">{f.arrival}</b></> : f.arrival}
                </td>
                <td>{f.stops === 0 ? "Nonstop" : `${f.stops} stop`}</td>
                <td className="num">
                  {dropped && <s className="was">{money(b.price)}</s>}
                  <span key={f.price} className={`fare ${moved.has(f.flight_id) ? "moved" : ""}`}>{money(f.price)}</span>
                </td>
                <td>
                  {isBooked ? (
                    <span className={`chip ${bookedInvalid ? "chip-bad" : "chip-booked"}`}>
                      {bookedInvalid ? `Booked, now ${f.violations?.map(reason).join(", ") || "invalid"}` : `Booked ${booking!.booking_id}`}
                    </span>
                  ) : f.violations === undefined ? (
                    <span className="chip chip-no">No trip to check yet</span>
                  ) : f.qualifies ? (
                    <span className="chip chip-ok">Qualifies</span>
                  ) : (
                    <span className="chip chip-no">Invalid: {f.violations?.map(reason).join(", ")}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
