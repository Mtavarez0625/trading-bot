"use client";

import { useState } from "react";
import type { BotEvent } from "@/lib/types";

interface Props {
  events: BotEvent[];
}

/* ── Event type → icon + label ──────────────────────────────────────── */
const EVENT_META: Record<string, { icon: string; label: string; badge: string }> = {
  trade_entered:        { icon: "↑",  label: "Trade Entered",       badge: "bg-emerald-950/70 text-emerald-300 ring-1 ring-emerald-800/60" },
  trade_exited:         { icon: "↓",  label: "Trade Exited",        badge: "bg-blue-950/70   text-blue-300   ring-1 ring-blue-800/60"    },
  stop_loss_hit:        { icon: "✕",  label: "Stop Loss Hit",       badge: "bg-red-950/70    text-red-300    ring-1 ring-red-800/60"      },
  take_profit_hit:      { icon: "✓",  label: "Take Profit Hit",     badge: "bg-emerald-950/70 text-emerald-300 ring-1 ring-emerald-800/60" },
  bot_flattened:        { icon: "◉",  label: "Bot Flattened",       badge: "bg-amber-950/70  text-amber-300  ring-1 ring-amber-800/60"   },
  scan_cycle_completed: { icon: "◎",  label: "Scan Cycle",          badge: "bg-[#192c40]     text-[#5f7d97]  ring-1 ring-[#192c40]"      },
  daily_loss_shutdown:  { icon: "⛔", label: "Daily Loss Shutdown", badge: "bg-red-950/70    text-red-300    ring-1 ring-red-800/60"      },
  bot_started:          { icon: "◆",  label: "Bot Started",         badge: "bg-blue-950/70   text-blue-300   ring-1 ring-blue-800/60"    },
  bot_stopped:          { icon: "□",  label: "Bot Stopped",         badge: "bg-[#192c40]     text-slate-400  ring-1 ring-[#192c40]"      },
  partial_tp_hit:       { icon: "½",  label: "Partial TP Hit",      badge: "bg-emerald-950/70 text-emerald-400 ring-1 ring-emerald-800/60" },
  breakeven_set:        { icon: "═",  label: "Breakeven Set",       badge: "bg-blue-950/70   text-blue-300   ring-1 ring-blue-800/60"    },
};

const SEVERITY_TEXT: Record<string, string> = {
  error:   "text-red-400",
  warning: "text-amber-400",
  success: "text-emerald-300",
  info:    "text-slate-300",
};

function fmtTime(ts: string) {
  try {
    return new Date(ts).toLocaleTimeString("en-US", {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      timeZone: "America/New_York", hour12: false,
    });
  } catch { return ts; }
}

function fmtDate(ts: string) {
  try {
    return new Date(ts).toLocaleDateString("en-US", {
      month: "short", day: "numeric",
      timeZone: "America/New_York",
    });
  } catch { return ""; }
}

function isScanEvent(ev: BotEvent) {
  return ev.event_type === "scan_cycle_completed";
}

/* ── Single event row ───────────────────────────────────────────────── */
function EventRow({ ev, idx }: { ev: BotEvent; idx: number }) {
  const [expanded, setExpanded] = useState(false);
  const meta = EVENT_META[ev.event_type];
  const icon  = meta?.icon  ?? "·";
  const badge = meta?.badge ?? "bg-[#192c40] text-slate-400 ring-1 ring-[#192c40]";
  const label = meta?.label ?? ev.event_type.replace(/_/g, " ");
  const isScan = isScanEvent(ev);
  const msgColor = SEVERITY_TEXT[ev.severity] ?? "text-slate-400";

  return (
    <div
      className={`flex items-start gap-3 py-2 border-b border-[#192c40]/50 last:border-0 transition-colors group
        ${isScan ? "opacity-50 hover:opacity-100" : ""}`}
      style={{ animationDelay: `${idx * 20}ms` }}
    >
      {/* Timestamp */}
      <span className="shrink-0 text-[10px] text-[#364f63] tabular-nums w-[68px] pt-0.5 group-hover:text-[#5f7d97] transition-colors">
        {fmtTime(ev.timestamp_utc)}
      </span>

      {/* Event type badge */}
      <span className={`shrink-0 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold ${badge}`}>
        <span>{icon}</span>
        {!isScan && <span className="hidden sm:inline">{label}</span>}
        {isScan && <span>Scan</span>}
      </span>

      {/* Symbol */}
      {ev.symbol && !isScan && (
        <span className="shrink-0 font-bold text-slate-200 text-[12px]">{ev.symbol}</span>
      )}
      {ev.symbol && isScan && (
        <span className="shrink-0 text-[#5f7d97] text-[11px]">{ev.symbol}</span>
      )}

      {/* Score + grade (for scan events) */}
      {ev.score != null && (
        <span className="shrink-0 text-[10px] text-amber-400 font-semibold">{ev.score}</span>
      )}
      {ev.grade && (
        <span className="shrink-0 text-[10px] text-slate-400">[{ev.grade}]</span>
      )}

      {/* Message */}
      <button
        className={`flex-1 text-left text-[11px] leading-relaxed min-w-0 cursor-pointer ${msgColor}`}
        onClick={() => setExpanded(x => !x)}
        title={expanded ? "Collapse" : "Expand"}
      >
        <span className={`${expanded ? "" : "line-clamp-1"}`}>{ev.message}</span>
        {ev.blocked_by && !expanded && (
          <span className="ml-1 text-amber-500/70 text-[10px]">· {ev.blocked_by.replace(/_/g, " ")}</span>
        )}
        {ev.blocked_by && expanded && (
          <div className="mt-0.5 text-[10px] text-amber-400/80">
            Blocked by: {ev.blocked_by.replace(/_/g, " ")}
          </div>
        )}
      </button>
    </div>
  );
}

/* ── Panel ─────────────────────────────────────────────────────────── */
export default function EventTimeline({ events }: Props) {
  const [showScans, setShowScans] = useState(false);
  const [filter, setFilter] = useState<string>("all");

  const significant = events.filter(e => !isScanEvent(e));
  const visible = (showScans ? events : significant)
    .filter(e => filter === "all" || e.severity === filter)
    .slice(0, 80);

  const errorCount   = events.filter(e => e.severity === "error").length;
  const successCount = events.filter(e => e.severity === "success").length;
  const tradeCount   = events.filter(e =>
    ["trade_entered", "trade_exited", "stop_loss_hit", "take_profit_hit"].includes(e.event_type)
  ).length;

  return (
    <div className="panel flex flex-col h-full">
      {/* Header */}
      <div className="flex items-start justify-between mb-4 flex-wrap gap-2">
        <div>
          <div className="panel-title mb-1">
            Event Timeline
            <span className="ml-2 rounded-full bg-[#192c40] px-1.5 py-0.5 text-[#5f7d97] text-[10px] font-normal normal-case tracking-normal">
              {events.length}
            </span>
          </div>
          {/* Quick stats */}
          <div className="flex items-center gap-3 text-[10px]">
            {tradeCount > 0 && (
              <span className="text-emerald-400">{tradeCount} trade{tradeCount !== 1 ? "s" : ""}</span>
            )}
            {errorCount > 0 && (
              <span className="text-red-400">{errorCount} error{errorCount !== 1 ? "s" : ""}</span>
            )}
            {successCount > 0 && (
              <span className="text-emerald-500/60">{successCount} successes</span>
            )}
          </div>
        </div>

        {/* Controls */}
        <div className="flex items-center gap-2 flex-wrap">
          {/* Severity filter */}
          {(["all", "error", "warning", "success", "info"] as const).map(sev => (
            <button
              key={sev}
              onClick={() => setFilter(sev)}
              className={`text-[9px] font-bold uppercase tracking-widest px-2 py-1 rounded transition-colors ${
                filter === sev
                  ? "bg-[#192c40] text-slate-200"
                  : "text-[#364f63] hover:text-slate-400"
              }`}
            >
              {sev}
            </button>
          ))}
          {/* Scan toggle */}
          <button
            onClick={() => setShowScans(s => !s)}
            className={`text-[9px] font-bold uppercase tracking-widest px-2 py-1 rounded transition-colors ${
              showScans
                ? "bg-blue-950/60 text-blue-300 ring-1 ring-blue-800/60"
                : "text-[#364f63] hover:text-slate-400"
            }`}
          >
            +Scans
          </button>
        </div>
      </div>

      {/* Event list */}
      <div className="flex-1 overflow-y-auto space-y-0 pr-1" style={{ maxHeight: "460px" }}>
        {visible.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-[#364f63]">
            <div className="text-2xl mb-2">◎</div>
            <div className="text-[11px]">No events match this filter</div>
          </div>
        ) : (
          visible.map((ev, i) => <EventRow key={`${ev.timestamp_utc}-${i}`} ev={ev} idx={i} />)
        )}
      </div>
    </div>
  );
}
