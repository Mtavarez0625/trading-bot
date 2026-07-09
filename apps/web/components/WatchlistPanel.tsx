"use client";

import type { BotEvent } from "@/lib/types";

interface Props {
  watchlist: string[];
  events: BotEvent[];
}

interface SymbolData {
  symbol: string;
  lastScore: number | null;
  grade: string | null;
  blockedBy: string | null;
  lastSeen: string | null;
}

/* ── Derive per-symbol data from scan events ────────────────────────── */
function buildSymbolData(watchlist: string[], events: BotEvent[]): SymbolData[] {
  const latest = new Map<string, BotEvent>();

  for (const ev of events) {
    if (ev.event_type !== "scan_cycle_completed" || !ev.symbol) continue;
    const existing = latest.get(ev.symbol);
    if (!existing || ev.timestamp_utc > existing.timestamp_utc) {
      latest.set(ev.symbol, ev);
    }
  }

  return watchlist.map(sym => {
    const ev = latest.get(sym);
    return {
      symbol: sym,
      lastScore: ev?.score ?? null,
      grade: ev?.grade ?? null,
      blockedBy: ev?.blocked_by ?? null,
      lastSeen: ev?.timestamp_utc ?? null,
    };
  });
}

/* ── Grade styling ─────────────────────────────────────────────────── */
function gradeStyle(grade: string | null): { badge: string; score: string; bar: string } {
  switch (grade) {
    case "A+": return { badge: "bg-emerald-950 text-emerald-200 ring-1 ring-emerald-600", score: "text-emerald-300", bar: "bg-emerald-500" };
    case "A":  return { badge: "bg-emerald-950 text-emerald-300 ring-1 ring-emerald-700", score: "text-emerald-400", bar: "bg-emerald-400" };
    case "B":  return { badge: "bg-blue-950    text-blue-300    ring-1 ring-blue-700",    score: "text-blue-300",    bar: "bg-blue-400"   };
    case "C":  return { badge: "bg-amber-950   text-amber-300   ring-1 ring-amber-700",   score: "text-amber-300",   bar: "bg-amber-400"  };
    case "D":  return { badge: "bg-orange-950  text-orange-400  ring-1 ring-orange-700",  score: "text-orange-400",  bar: "bg-orange-500" };
    case "F":  return { badge: "bg-red-950     text-red-400     ring-1 ring-red-700",     score: "text-red-400",     bar: "bg-red-500"    };
    default:   return { badge: "bg-[#192c40]   text-slate-500   ring-1 ring-[#192c40]",   score: "text-slate-600",   bar: "bg-slate-700"  };
  }
}

function fmtTime(ts: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleTimeString("en-US", {
      hour: "2-digit", minute: "2-digit",
      timeZone: "America/New_York",
      hour12: false,
    }) + " ET";
  } catch { return "—"; }
}

/* ── Symbol card ───────────────────────────────────────────────────── */
function SymbolCard({ data }: { data: SymbolData }) {
  const style = gradeStyle(data.grade);
  const hasData = data.lastScore !== null;

  return (
    <div className="bg-[#0e1929] border border-[#192c40] rounded-xl p-4 hover:border-[#243d58] transition-all duration-150 group">
      {/* Top row: ticker + grade */}
      <div className="flex items-start justify-between mb-3">
        <span className="text-white font-bold text-[16px] tracking-wider group-hover:text-blue-300 transition-colors">
          {data.symbol}
        </span>
        {data.grade ? (
          <span className={`inline-flex items-center justify-center rounded-md px-2.5 py-1 text-[13px] font-bold ${style.badge}`}>
            {data.grade}
          </span>
        ) : (
          <span className="chip chip-muted text-[9px]">NO DATA</span>
        )}
      </div>

      {/* Score bar */}
      <div className="mb-3">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[9px] text-[#364f63] uppercase tracking-widest">Score</span>
          <span className={`text-[13px] font-bold tabular-nums ${style.score}`}>
            {hasData ? data.lastScore : "—"}
          </span>
        </div>
        <div className="h-1 bg-[#192c40] rounded-full overflow-hidden">
          <div
            className={`h-full ${style.bar} rounded-full transition-all duration-700 opacity-70`}
            style={{ width: hasData ? `${data.lastScore}%` : "0%" }}
          />
        </div>
      </div>

      {/* Blocked reason or clean */}
      {data.blockedBy ? (
        <div className="text-[10px] text-amber-400/80 bg-amber-950/30 rounded px-2 py-1 truncate" title={data.blockedBy}>
          ⚠ {data.blockedBy.replace(/_/g, " ")}
        </div>
      ) : hasData ? (
        <div className="text-[10px] text-emerald-500/70">✓ No blockers</div>
      ) : (
        <div className="text-[10px] text-[#364f63]">Not yet scanned</div>
      )}

      {/* Last seen */}
      <div className="mt-2 text-[9px] text-[#364f63]">
        {data.lastSeen ? `Last scan ${fmtTime(data.lastSeen)}` : "Awaiting scan"}
      </div>
    </div>
  );
}

/* ── Panel ─────────────────────────────────────────────────────────── */
export default function WatchlistPanel({ watchlist, events }: Props) {
  if (watchlist.length === 0) return null;

  const symbolData = buildSymbolData(watchlist, events);
  const scanned = symbolData.filter(s => s.grade !== null).length;
  const topCandidate = symbolData
    .filter(s => s.lastScore !== null)
    .sort((a, b) => (b.lastScore ?? 0) - (a.lastScore ?? 0))[0] ?? null;

  return (
    <div className="panel">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="panel-title mb-0">
          Watchlist
          <span className="ml-2 text-[#364f63] normal-case tracking-normal font-normal">
            ({scanned}/{watchlist.length} scanned)
          </span>
        </div>
        {topCandidate && (
          <div className="text-[10px] text-[#5f7d97]">
            Top:{" "}
            <span className="text-amber-300 font-semibold">{topCandidate.symbol}</span>
            {topCandidate.grade && (
              <span className="ml-1 text-[#5f7d97]">({topCandidate.grade})</span>
            )}
            {topCandidate.lastScore !== null && (
              <span className="ml-1 text-slate-400">{topCandidate.lastScore}</span>
            )}
          </div>
        )}
      </div>

      {/* Symbol cards grid */}
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3">
        {symbolData.map(data => (
          <SymbolCard key={data.symbol} data={data} />
        ))}
      </div>
    </div>
  );
}
