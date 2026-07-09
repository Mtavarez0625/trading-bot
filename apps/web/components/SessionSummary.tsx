"use client";

import type { RuntimeStats, HermesSummary, DailyReport } from "@/lib/types";

interface Props {
  runtime: RuntimeStats | null;
  hermes: HermesSummary | null;
  dailyReport: DailyReport | null;
}

/* ── Signal funnel bar ──────────────────────────────────────────────── */
function FunnelBar({
  label,
  value,
  max,
  color,
  textColor,
}: {
  label: string;
  value: number | string;
  max: number;
  color: string;
  textColor: string;
}) {
  const n = typeof value === "number" ? value : 0;
  const pct = max > 0 ? Math.min((n / max) * 100, 100) : 0;

  return (
    <div className="flex items-center gap-3">
      <span className="w-20 text-[10px] text-[#5f7d97] shrink-0 text-right">{label}</span>
      <div className="flex-1 h-1.5 bg-[#192c40] rounded-full overflow-hidden">
        <div
          className={`h-full ${color} rounded-full transition-all duration-700`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={`text-[12px] font-bold tabular-nums w-8 text-right shrink-0 ${textColor}`}>
        {value === 0 ? <span className="text-[#364f63]">0</span> : value}
      </span>
    </div>
  );
}

/* ── Single stat tile ───────────────────────────────────────────────── */
function StatTile({
  label,
  value,
  color = "text-slate-100",
}: {
  label: string;
  value: string | number;
  color?: string;
}) {
  return (
    <div className="flex flex-col gap-1 min-w-[60px]">
      <span className="text-[9px] font-bold uppercase tracking-[0.16em] text-[#364f63]">{label}</span>
      <span className={`text-[22px] font-bold leading-none tabular-nums ${color}`}>{value}</span>
    </div>
  );
}

export default function SessionSummary({ runtime, hermes, dailyReport }: Props) {
  // Primary: /daily-report scan_state_summary — always current, survives bot restart
  // Fallback: /hermes/session-summary → runtime-stats
  const scan      = dailyReport?.scan_state_summary;
  const drSession = dailyReport?.session_stats;
  const hStats    = hermes?.session_stats;
  const mini      = runtime?.session_mini_stats;

  const scans     = scan?.total_scan_cycles        ?? hStats?.scan_cycles           ?? runtime?.total_scan_cycles ?? 0;
  const evaluated = scan?.total_symbols_evaluated  ?? mini?.total_symbols_evaluated ?? 0;
  const signals   = scan?.buy_signals_seen         ?? hStats?.buy_signals_seen      ?? 0;
  const blocked   = scan?.entries_blocked          ?? hStats?.entries_blocked       ?? 0;
  const entered   = scan?.entries_taken            ?? hStats?.entries_today         ?? mini?.entered ?? 0;
  const exits     = drSession?.exited              ?? hStats?.exits_today           ?? 0;
  const nearMiss  = drSession?.near_miss_count     ?? hStats?.near_misses           ?? mini?.near_misses ?? 0;
  const errors    = scan?.errors                   ?? hStats?.errors_today          ?? mini?.errors ?? 0;

  const funnelMax = Math.max(Number(scans), 1);

  return (
    <div className="panel">
      <div className="flex items-start justify-between flex-wrap gap-4 mb-4">
        <div className="panel-title mb-0">Session Activity</div>
        {runtime?.last_scan_at_et && (
          <span className="text-[10px] text-[#364f63]">
            Last scan: <span className="text-[#5f7d97]">{runtime.last_scan_at_et} ET</span>
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Signal funnel */}
        <div>
          <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-[#364f63] mb-3">
            Signal Funnel
          </div>
          <div className="space-y-2">
            <FunnelBar label="Scans"     value={scans}     max={funnelMax} color="bg-blue-500/40"    textColor="text-blue-300"    />
            <FunnelBar label="Evaluated" value={evaluated} max={funnelMax} color="bg-blue-400/30"    textColor="text-blue-400"    />
            <FunnelBar label="Signals"   value={signals}   max={funnelMax} color="bg-emerald-500/50" textColor="text-emerald-400" />
            <FunnelBar label="Blocked"   value={blocked}   max={funnelMax} color="bg-amber-500/40"   textColor="text-amber-400"   />
            <FunnelBar label="Entered"   value={entered}   max={funnelMax} color="bg-emerald-500/80" textColor="text-emerald-300" />
          </div>
        </div>

        {/* Quick stat tiles */}
        <div className="flex flex-wrap gap-x-6 gap-y-4 content-start">
          <StatTile label="Scans"      value={scans}    color="text-slate-200"    />
          <StatTile label="Signals"    value={signals}  color="text-emerald-400"  />
          <StatTile label="Entered"    value={entered}  color="text-emerald-300"  />
          <StatTile label="Exits"      value={exits}    color="text-blue-300"     />
          <StatTile label="Near Miss"  value={nearMiss} color="text-amber-400"    />
          <StatTile label="Blocked"    value={blocked}  color="text-amber-500"    />
          <StatTile
            label="Errors"
            value={errors}
            color={Number(errors) > 0 ? "text-red-400" : "text-slate-600"}
          />
          {runtime?.uptime_minutes != null && (
            <StatTile label="Uptime" value={`${runtime.uptime_minutes}m`} color="text-slate-400" />
          )}
        </div>
      </div>

      {runtime?.trading_window && (
        <div className="mt-3 pt-3 border-t border-[#192c40] text-[10px] text-[#364f63]">
          Trading window: <span className="text-[#5f7d97]">{runtime.trading_window}</span>
          {runtime.opening_momentum_mode && (
            <span className="ml-3 chip chip-purple text-[9px]">ORB MODE</span>
          )}
        </div>
      )}
    </div>
  );
}
