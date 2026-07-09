"use client";

import type { OpenPosition } from "@/lib/types";

interface Props {
  positions: OpenPosition[];
}

/* ── Helpers ────────────────────────────────────────────────────────── */
function fmtPrice(n: number | undefined | null): string {
  if (n == null) return "—";
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}

function fmtTime(ts: string | undefined): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleTimeString("en-US", {
      hour: "2-digit", minute: "2-digit",
      timeZone: "America/New_York", hour12: false,
    }) + " ET";
  } catch { return ts; }
}

function calcRR(
  entry: number | undefined,
  stop: number | undefined,
  target: number | undefined
): string {
  if (!entry || !stop || !target) return "—";
  const risk   = Math.abs(entry - stop);
  const reward = Math.abs(target - entry);
  if (risk === 0) return "—";
  return `${(reward / risk).toFixed(1)}R`;
}

function stopDistance(entry: number | undefined, stop: number | undefined): string {
  if (!entry || !stop) return "—";
  return `${Math.abs(((entry - stop) / entry) * 100).toFixed(2)}%`;
}

function targetDistance(entry: number | undefined, target: number | undefined): string {
  if (!entry || !target) return "—";
  return `+${Math.abs(((target - entry) / entry) * 100).toFixed(2)}%`;
}

/* ── Empty state ────────────────────────────────────────────────────── */
function EmptyPositions() {
  return (
    <div className="flex flex-col items-center justify-center py-10 text-[#364f63]">
      <div className="text-3xl mb-2 opacity-40">□</div>
      <div className="text-[12px]">No open positions</div>
      <div className="text-[10px] mt-1 text-[#364f63]">Bot is monitoring the watchlist</div>
    </div>
  );
}

export default function PositionsTable({ positions }: Props) {
  return (
    <div className="panel">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="panel-title mb-0">
          Open Positions
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-[11px] font-bold ${
              positions.length > 0
                ? "bg-emerald-950 text-emerald-300 ring-1 ring-emerald-800"
                : "bg-[#192c40] text-[#5f7d97]"
            }`}
          >
            {positions.length}
          </span>
          {positions.length > 0 && (
            <span className="chip chip-green text-[9px] tracking-widest animate-pulse">LIVE</span>
          )}
        </div>
      </div>

      {positions.length === 0 ? (
        <EmptyPositions />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="text-left border-b border-[#192c40]">
                {["Symbol", "Qty", "Entry", "Stop", "Target", "−Risk", "+Reward", "R/R", "Tier", "Entered"].map(h => (
                  <th
                    key={h}
                    className="pb-2.5 pr-4 text-[9px] font-bold uppercase tracking-[0.14em] text-[#364f63] last:pr-0"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map((pos, i) => {
                const rr   = calcRR(pos.entry_price, pos.stop_price, pos.take_profit_price);
                const risk = stopDistance(pos.entry_price, pos.stop_price);
                const rwd  = targetDistance(pos.entry_price, pos.take_profit_price);

                return (
                  <tr
                    key={`${pos.symbol}-${i}`}
                    className="border-b border-[#192c40]/50 last:border-0 hover:bg-white/[0.02] transition-colors"
                  >
                    <td className="py-3 pr-4">
                      <span className="font-bold text-white text-[13px] tracking-wide">{pos.symbol}</span>
                    </td>
                    <td className="py-3 pr-4 text-slate-300 font-semibold">{pos.qty}</td>
                    <td className="py-3 pr-4 text-slate-300 tabular-nums">{fmtPrice(pos.entry_price)}</td>
                    <td className="py-3 pr-4 text-red-400 tabular-nums font-semibold">{fmtPrice(pos.stop_price)}</td>
                    <td className="py-3 pr-4 text-emerald-400 tabular-nums font-semibold">{fmtPrice(pos.take_profit_price)}</td>
                    <td className="py-3 pr-4 text-red-400/80 tabular-nums">{risk}</td>
                    <td className="py-3 pr-4 text-emerald-400/80 tabular-nums">{rwd}</td>
                    <td className="py-3 pr-4">
                      <span className="font-bold text-amber-400 tabular-nums">{rr}</span>
                    </td>
                    <td className="py-3 pr-4">
                      {pos.entry_tier ? (
                        <span className="rounded-md bg-blue-950/60 px-2 py-1 text-blue-300 text-[10px] font-semibold ring-1 ring-blue-900/60">
                          {pos.entry_tier}
                        </span>
                      ) : (
                        <span className="text-[#364f63]">—</span>
                      )}
                    </td>
                    <td className="py-3 text-[#5f7d97] tabular-nums">{fmtTime(pos.entry_timestamp)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
