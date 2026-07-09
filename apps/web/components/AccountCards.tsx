"use client";

import type { Account, DashboardData, HermesSummary, DailyReport } from "@/lib/types";

interface Props {
  account: Account | null;
  dashboard: DashboardData | null;
  hermes: HermesSummary | null;
  dailyReport: DailyReport | null;
}

/* ── Helpers ──────────────────────────────────────────────────────────── */
function fmtDollar(n: number | null, opts = { sign: false }): string {
  if (n === null) return "—";
  const abs = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (!opts.sign) return `$${abs}`;
  const prefix = n >= 0 ? "+" : "−";
  return `${prefix}$${abs}`;
}

function pnlClass(n: number | null): string {
  if (n === null) return "text-slate-400";
  return n > 0 ? "text-emerald-400" : n < 0 ? "text-red-400" : "text-slate-300";
}

/* ── KPI Card ─────────────────────────────────────────────────────────── */
function KpiCard({
  label,
  value,
  valueClass = "text-white",
  sub,
  subClass = "text-[#5f7d97]",
  accent,
  tag,
}: {
  label: string;
  value: string | number;
  valueClass?: string;
  sub?: string;
  subClass?: string;
  accent?: string;
  tag?: { text: string; cls: string };
}) {
  return (
    <div className="kpi-card animate-fade-in-up">
      {/* Top label row */}
      <div className="flex items-start justify-between mb-3">
        <span className="text-[10px] text-[#5f7d97] font-bold uppercase tracking-[0.16em]">{label}</span>
        {tag && (
          <span className={`chip text-[9px] ${tag.cls}`}>{tag.text}</span>
        )}
      </div>

      {/* Main metric */}
      <div className={`text-[28px] font-bold leading-none tracking-tight tabular-nums ${valueClass}`}>
        {value}
      </div>

      {/* Accent bar (colour strip under value) */}
      {accent && (
        <div className={`mt-2 h-0.5 w-8 rounded-full ${accent} opacity-60`} />
      )}

      {/* Secondary info */}
      {sub && (
        <div className={`mt-2 text-[10px] leading-relaxed truncate ${subClass}`}>{sub}</div>
      )}
    </div>
  );
}

/* ── Main export ──────────────────────────────────────────────────────── */
export default function AccountCards({ account, dashboard, hermes, dailyReport }: Props) {
  const equity        = account?.equity        ? parseFloat(account.equity)        : null;
  const bp            = account?.buying_power  ? parseFloat(account.buying_power)  : null;
  const dailyPnl      = dashboard?.daily_pnl   ?? null;
  const realizedPnl   = dashboard?.realized_pnl ?? null;
  const unrealizedPnl = dashboard?.unrealized_pnl ?? null;
  const positions     = dashboard?.open_positions ?? [];

  // Primary: /daily-report  — populated as long as the API is up (survives bot restart)
  // Fallback: /hermes/session-summary (60s poll, may be stale after restart)
  const scan      = dailyReport?.scan_state_summary;
  const drSession = dailyReport?.session_stats;
  const hStats    = hermes?.session_stats;

  const buySignals = scan?.buy_signals_seen    ?? hStats?.buy_signals_seen ?? null;
  const nearMisses = drSession?.near_miss_count ?? hStats?.near_misses     ?? null;
  const errors     = scan?.errors               ?? hStats?.errors_today    ?? null;

  /* P&L secondary */
  const pnlSub = (realizedPnl !== null && unrealizedPnl !== null)
    ? `real ${fmtDollar(realizedPnl, { sign: true })} · unreal ${fmtDollar(unrealizedPnl, { sign: true })}`
    : undefined;

  return (
    <div className="flex flex-wrap gap-3">

      {/* 1 — Equity */}
      <KpiCard
        label="Account Equity"
        value={equity !== null ? fmtDollar(equity) : "—"}
        valueClass="text-white"
        sub="paper account"
        accent="bg-blue-400"
      />

      {/* 2 — Buying Power */}
      <KpiCard
        label="Buying Power"
        value={bp !== null ? fmtDollar(bp) : "—"}
        valueClass="text-slate-200"
        sub="available capital"
        accent="bg-slate-400"
      />

      {/* 3 — Daily P&L */}
      <KpiCard
        label="Today's P&L"
        value={dailyPnl !== null ? fmtDollar(dailyPnl, { sign: true }) : "—"}
        valueClass={pnlClass(dailyPnl)}
        sub={pnlSub}
        accent={dailyPnl !== null ? (dailyPnl >= 0 ? "bg-emerald-400" : "bg-red-400") : "bg-slate-600"}
      />

      {/* 4 — Open Positions */}
      <KpiCard
        label="Open Positions"
        value={positions.length}
        valueClass={positions.length > 0 ? "text-emerald-300" : "text-slate-500"}
        sub={positions.length > 0 ? positions.map(p => p.symbol).join(" · ") : "no open positions"}
        accent={positions.length > 0 ? "bg-emerald-400" : "bg-slate-700"}
      />

      {/* 5 — Buy Signals */}
      <KpiCard
        label="Buy Signals"
        value={buySignals ?? "—"}
        valueClass="text-emerald-400"
        sub="signals seen today"
        accent="bg-emerald-500"
        tag={buySignals !== null && buySignals > 0 ? { text: "ACTIVE", cls: "chip-green" } : undefined}
      />

      {/* 6 — Near Misses */}
      <KpiCard
        label="Near Misses"
        value={nearMisses ?? "—"}
        valueClass="text-amber-400"
        sub="just below threshold"
        accent="bg-amber-500"
      />

      {/* 7 — Errors */}
      <KpiCard
        label="Errors"
        value={errors ?? "—"}
        valueClass={errors !== null && errors > 0 ? "text-red-400" : "text-slate-500"}
        sub={errors !== null && errors > 0 ? "review event log" : "clean session"}
        accent={errors !== null && errors > 0 ? "bg-red-500" : "bg-emerald-700"}
        tag={errors !== null && errors > 0 ? { text: "REVIEW", cls: "chip-red" } : undefined}
      />

    </div>
  );
}
