"use client";

import type { DashboardData, RuntimeStats, HermesSummary } from "@/lib/types";

interface Props {
  dashboard: DashboardData | null;
  runtime: RuntimeStats | null;
  hermes: HermesSummary | null;
  lastRefresh: Date | null;
  apiOnline: boolean;
  etTime: string;
  onRefresh: () => void;
}

/* Animated live ping indicator */
function LivePing({ color }: { color: string }) {
  return (
    <span className="relative inline-flex w-2.5 h-2.5 shrink-0">
      <span className={`animate-ping absolute inline-flex h-full w-full rounded-full ${color} opacity-50`} />
      <span className={`relative inline-flex rounded-full w-2.5 h-2.5 ${color}`} />
    </span>
  );
}

function StaticDot({ color }: { color: string }) {
  return <span className={`inline-block w-2.5 h-2.5 rounded-full shrink-0 ${color}`} />;
}

function StatusPill({
  ok,
  label,
  live = false,
}: {
  ok: boolean | null;
  label: string;
  live?: boolean;
}) {
  const color =
    ok === true  ? "bg-emerald-400" :
    ok === false ? "bg-red-500"     : "bg-amber-400";
  const text =
    ok === true  ? "text-slate-300"  :
    ok === false ? "text-red-400"    : "text-amber-400";

  return (
    <span className={`flex items-center gap-1.5 text-[11px] font-medium ${text}`}>
      {live && ok === true ? <LivePing color={color} /> : <StaticDot color={color} />}
      {label}
    </span>
  );
}

export default function StatusBar({
  dashboard,
  runtime,
  hermes,
  lastRefresh,
  apiOnline,
  etTime,
  onRefresh,
}: Props) {
  const botRunning    = dashboard?.bot_running ?? null;
  const marketOpen    = dashboard?.market_open ?? runtime?.market_is_open ?? null;
  const insideWindow  = runtime?.inside_trading_window ?? false;
  const paperMode     = dashboard?.paper_mode ?? runtime?.safety_flags?.alpaca_paper ?? null;
  const alertsEnabled = dashboard?.alerts_enabled ?? null;
  const dailyShutdown = dashboard?.daily_loss_shutdown ?? false;
  const sessionFlat   = runtime?.safety_flags?.session_flattened ?? false;
  const hermesLoaded  = hermes !== null;

  const timeSince = lastRefresh
    ? Math.round((Date.now() - lastRefresh.getTime()) / 1000)
    : null;

  const marketLabel =
    marketOpen === true  ? "Market Open"   :
    marketOpen === false ? "Market Closed" : "Market";

  const windowLabel = insideWindow ? "Window Active" : "Outside Window";

  return (
    <div className="rounded-xl overflow-hidden bg-[#0e1929] border border-[#192c40]">
      {/* ── Top row: Brand · Clock · Refresh ───────────────────────────── */}
      <div className="px-5 py-4 flex flex-wrap items-center justify-between gap-4">

        {/* Brand */}
        <div className="flex flex-col gap-1 min-w-0">
          <div className="flex items-center gap-2.5 flex-wrap">
            <span className="text-blue-400 text-xl font-bold leading-none select-none">◆</span>
            <span className="text-white font-bold text-[15px] tracking-wider select-none">
              MISSION CONTROL
            </span>
            <span className="h-4 w-px bg-[#192c40]" />
            <span className="text-[10px] text-[#5f7d97] uppercase tracking-[0.18em]">
              Trading Module
            </span>
            {paperMode && (
              <span className="chip chip-blue text-[9px] tracking-widest">PAPER</span>
            )}
          </div>
          <div className="text-[10px] text-[#364f63] tracking-wide">
            Read-Only Observation · No Trading Controls
          </div>
        </div>

        {/* Live ET clock */}
        <div className="flex flex-col items-center gap-0.5 tabular-nums">
          <div className="text-[28px] font-bold text-white tracking-[0.05em] leading-none">
            {etTime || dashboard?.current_et_time || runtime?.current_et_time || "——:——:——"}
          </div>
          <div className="text-[9px] text-[#364f63] uppercase tracking-[0.22em]">
            Eastern Time
          </div>
        </div>

        {/* Refresh */}
        <div className="flex flex-col items-end gap-2">
          <button
            onClick={onRefresh}
            className="rounded-lg px-4 py-2 text-[11px] font-semibold text-slate-300 bg-[#0a1320] border border-[#192c40] hover:border-[#243d58] hover:text-white transition-all duration-150 active:scale-95"
          >
            ↻ Refresh
          </button>
          {timeSince !== null && (
            <span className="text-[10px] text-[#364f63]">
              {timeSince < 4 ? "just refreshed" : `${timeSince}s ago`}
            </span>
          )}
        </div>
      </div>

      {/* ── Status strip ────────────────────────────────────────────────── */}
      <div className="px-5 py-2.5 bg-[#0a1320] border-t border-[#192c40] flex flex-wrap items-center justify-between gap-3">
        {/* Health indicators */}
        <div className="flex flex-wrap items-center gap-4">
          <StatusPill ok={apiOnline}    label="API"           live />
          <StatusPill ok={botRunning}   label="Bot"           live={botRunning === true} />
          <StatusPill ok={marketOpen}   label={marketLabel} />
          <StatusPill ok={insideWindow} label={windowLabel} />
          <StatusPill ok={hermesLoaded ? true : null} label="Hermes" />
          <StatusPill ok={alertsEnabled} label="Alerts" />
        </div>

        {/* Right: alerts + next_action + time */}
        <div className="flex flex-wrap items-center gap-3">
          {dailyShutdown && (
            <span className="chip chip-red text-[9px] tracking-widest animate-pulse">
              ✕ DAILY SHUTDOWN
            </span>
          )}
          {sessionFlat && (
            <span className="chip chip-amber text-[9px] tracking-widest">
              ⚠ SESSION FLATTENED
            </span>
          )}
          {runtime?.next_action && !dailyShutdown && !sessionFlat && (
            <span className="hidden md:block text-[10px] text-[#5f7d97] max-w-[300px] truncate">
              {runtime.next_action}
            </span>
          )}
          {lastRefresh && (
            <span className="text-[10px] text-[#364f63]">
              {lastRefresh.toLocaleTimeString()}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
