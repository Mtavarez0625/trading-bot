"use client";

import type { DashboardData, RuntimeStats } from "@/lib/types";

interface Props {
  dashboard: DashboardData | null;
  runtime: RuntimeStats | null;
}

/* ── Lock badge (green = safe) ──────────────────────────────────────── */
function LockBadge({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2.5 rounded-lg bg-emerald-950/40 ring-1 ring-emerald-900/60 px-3 py-2">
      <span className="text-emerald-400 text-[14px] shrink-0">🔒</span>
      <span className="text-[11px] text-emerald-300 font-medium">{label}</span>
    </div>
  );
}

/* ── Warning badge (red = active concern) ────────────────────────────── */
function WarnBadge({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2.5 rounded-lg bg-red-950/40 ring-1 ring-red-900/60 px-3 py-2">
      <span className="text-red-400 text-[14px] shrink-0">⚠</span>
      <span className="text-[11px] text-red-300 font-medium">{label}</span>
    </div>
  );
}

/* ── Risk parameter row ─────────────────────────────────────────────── */
function RiskRow({
  label,
  value,
  valueClass = "text-slate-200",
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-[#192c40]/60 last:border-0">
      <span className="text-[11px] text-[#5f7d97]">{label}</span>
      <span className={`text-[11px] font-semibold tabular-nums ${valueClass}`}>{value}</span>
    </div>
  );
}

export default function RiskPanel({ dashboard, runtime }: Props) {
  const flags           = runtime?.safety_flags;
  const liveLocked      = !flags?.allow_live_trading;
  const paperMode       = dashboard?.paper_mode ?? flags?.alpaca_paper;
  const dryRun          = dashboard?.dry_run ?? flags?.dry_run;
  const dailyShutdown   = dashboard?.daily_loss_shutdown ?? false;
  const sessionFlat     = flags?.session_flattened ?? false;
  const disabledEntries = flags?.disable_new_entries ?? false;
  const observeOnly     = flags?.observe_only_mode ?? false;
  const watchlist       = runtime?.watchlist ?? [];
  const window          = runtime?.trading_window;
  const uptime          = runtime?.uptime_minutes;
  const scanCadence     = runtime?.scan_cadence_seconds;

  return (
    <div className="panel flex flex-col gap-4">
      <div className="panel-title">Risk &amp; Safety</div>

      {/* Hard safety locks */}
      <div className="space-y-2">
        {liveLocked  && <LockBadge label="Live trading locked — ALLOW_LIVE_TRADING=false" />}
        {paperMode   && <LockBadge label="Paper mode active — no real money at risk"      />}
        {dryRun      && <LockBadge label="DRY_RUN mode — orders simulated only"           />}
      </div>

      {/* Active alerts */}
      {(dailyShutdown || sessionFlat || disabledEntries || observeOnly) && (
        <div className="space-y-2">
          {dailyShutdown   && <WarnBadge label="Daily loss limit hit — no new entries today" />}
          {sessionFlat     && <WarnBadge label="Session flattened — all positions closed"    />}
          {disabledEntries && <WarnBadge label="New entries disabled"                        />}
          {observeOnly     && <WarnBadge label="Observe-only mode active"                    />}
        </div>
      )}

      {/* Risk parameters */}
      <div>
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-[#364f63] mb-2">
          Parameters
        </div>
        <RiskRow label="Max Positions"      value="3"       />
        <RiskRow label="Risk Per Trade"     value="0.5%"    />
        <RiskRow label="Stop Loss"          value="2.0%"    valueClass="text-red-400"     />
        <RiskRow label="Take Profit"        value="4.0%"    valueClass="text-emerald-400" />
        <RiskRow label="Daily Loss Limit"   value="3.0%"    valueClass="text-red-400"     />
        <RiskRow label="Max Spread"         value="0.30%"   />
        {window      && <RiskRow label="Trading Window"   value={window}         />}
        {scanCadence && <RiskRow label="Scan Cadence"     value={`${scanCadence}s`} />}
        {uptime      != null && <RiskRow label="Uptime"    value={`${uptime}m`}  />}
      </div>

      {/* Watchlist */}
      {watchlist.length > 0 && (
        <div>
          <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-[#364f63] mb-2">
            Watchlist
          </div>
          <div className="flex flex-wrap gap-1.5">
            {watchlist.map(sym => (
              <span
                key={sym}
                className="rounded-md bg-[#0a1320] ring-1 ring-[#192c40] px-2 py-1 text-[11px] font-semibold text-slate-300"
              >
                {sym}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Footer */}
      <div className="mt-auto pt-2 border-t border-[#192c40] text-[9px] text-[#364f63]">
        Parameters are read from the bot config. Dashboard cannot modify any settings.
      </div>
    </div>
  );
}
