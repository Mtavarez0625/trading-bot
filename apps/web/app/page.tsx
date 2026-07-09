"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { api } from "@/lib/api";
import type {
  DashboardData,
  Account,
  RuntimeStats,
  HermesSummary,
  BotEvent,
  DailyReport,
  EndOfDayReview,
} from "@/lib/types";

import StatusBar from "@/components/StatusBar";
import AccountCards from "@/components/AccountCards";
import SessionSummary from "@/components/SessionSummary";
import PositionsTable from "@/components/PositionsTable";
import EventTimeline from "@/components/EventTimeline";
import HermesPanel from "@/components/HermesPanel";
import RiskPanel from "@/components/RiskPanel";
import WatchlistPanel from "@/components/WatchlistPanel";

/* ── Polling intervals — unchanged from original ───────────────────── */
const FAST_MS   = 15_000;
const SLOW_MS   = 30_000;
const HERMES_MS = 60_000;
const EOD_MS    = 300_000; // end-of-day review — slow-moving, no need to poll often

/* ── Market status banner ───────────────────────────────────────────── */
function MarketBanner({
  dashboard,
  runtime,
}: {
  dashboard: DashboardData | null;
  runtime: RuntimeStats | null;
}) {
  const isOpen       = dashboard?.market_open ?? runtime?.market_is_open;
  const insideWindow = runtime?.inside_trading_window ?? false;
  const flattened    = runtime?.safety_flags?.session_flattened ?? false;
  const shutdown     = dashboard?.daily_loss_shutdown ?? false;
  const dryRun       = dashboard?.dry_run ?? runtime?.safety_flags?.dry_run ?? false;
  const paperMode    = dashboard?.paper_mode ?? runtime?.safety_flags?.alpaca_paper ?? false;

  let label: string;
  let cls: string;
  let dotColor: string;
  let live = false;

  if (shutdown) {
    label    = "DAILY LOSS SHUTDOWN — ALL NEW ENTRIES HALTED";
    cls      = "bg-red-950/50 border-red-900/60 text-red-300";
    dotColor = "bg-red-400";
  } else if (flattened) {
    label    = "SESSION FLATTENED — ALL POSITIONS HAVE BEEN CLOSED";
    cls      = "bg-amber-950/50 border-amber-900/60 text-amber-300";
    dotColor = "bg-amber-400";
  } else if (!isOpen && isOpen !== null) {
    label    = "MARKET CLOSED — MONITORING PAUSED";
    cls      = "bg-[#0a1320] border-[#192c40] text-[#364f63]";
    dotColor = "bg-[#364f63]";
  } else if (insideWindow) {
    label    = dryRun
      ? "ACTIVE · DRY RUN MODE · SCANNING WATCHLIST"
      : "ACTIVE · PAPER TRADING · SCANNING WATCHLIST";
    cls      = "bg-emerald-950/40 border-emerald-900/50 text-emerald-300";
    dotColor = "bg-emerald-400";
    live     = true;
  } else if (isOpen) {
    label    = "MARKET OPEN · OUTSIDE TRADING WINDOW · OBSERVE ONLY";
    cls      = "bg-[#0e1929] border-[#192c40] text-[#5f7d97]";
    dotColor = "bg-[#5f7d97]";
  } else {
    label    = "INITIALIZING · AWAITING DATA";
    cls      = "bg-[#0a1320] border-[#192c40] text-[#364f63]";
    dotColor = "bg-[#364f63]";
  }

  const tags = [
    paperMode && "PAPER MODE",
    dryRun    && "DRY RUN",
    runtime?.trading_window  && `WINDOW ${runtime.trading_window}`,
    runtime?.scan_cadence_seconds && `SCAN ${runtime.scan_cadence_seconds}s`,
  ].filter(Boolean) as string[];

  return (
    <div className={`rounded-xl px-5 py-3 flex items-center justify-between border ${cls}`}>
      {/* Left: status */}
      <div className="flex items-center gap-3">
        {live ? (
          <span className="relative inline-flex w-2.5 h-2.5 shrink-0">
            <span className={`animate-ping absolute inline-flex h-full w-full rounded-full ${dotColor} opacity-50`} />
            <span className={`relative inline-flex rounded-full w-2.5 h-2.5 ${dotColor}`} />
          </span>
        ) : (
          <span className={`inline-block w-2.5 h-2.5 rounded-full shrink-0 ${dotColor}`} />
        )}
        <span className="text-[11px] font-bold tracking-[0.1em] uppercase">{label}</span>
      </div>
      {/* Right: tags */}
      {tags.length > 0 && (
        <div className="hidden sm:flex items-center gap-4">
          {tags.map(t => (
            <span key={t} className="text-[9px] font-bold uppercase tracking-[0.16em] opacity-50">
              {t}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Home page ──────────────────────────────────────────────────────── */
export default function Home() {
  const [dashboard,   setDashboard]   = useState<DashboardData | null>(null);
  const [account,     setAccount]     = useState<Account | null>(null);
  const [runtime,     setRuntime]     = useState<RuntimeStats | null>(null);
  const [hermes,      setHermes]      = useState<HermesSummary | null>(null);
  const [eodReview,   setEodReview]   = useState<EndOfDayReview | null>(null);
  const [dailyReport, setDailyReport] = useState<DailyReport | null>(null);
  const [events,      setEvents]      = useState<BotEvent[]>([]);
  const [apiOnline,   setApiOnline]   = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [etTime,      setEtTime]      = useState("");

  const fastTimer   = useRef<ReturnType<typeof setInterval> | null>(null);
  const slowTimer   = useRef<ReturnType<typeof setInterval> | null>(null);
  const hermesTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const eodTimer    = useRef<ReturnType<typeof setInterval> | null>(null);

  /* Live ET clock — updates every second */
  useEffect(() => {
    const tick = () =>
      setEtTime(
        new Date().toLocaleTimeString("en-US", {
          hour: "2-digit", minute: "2-digit", second: "2-digit",
          timeZone: "America/New_York", hour12: false,
        })
      );
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  const fetchFast = useCallback(async () => {
    const [dash, evts] = await Promise.all([api.dashboard(), api.events(100)]);
    if (dash) { setDashboard(dash); setApiOnline(true); }
    else        setApiOnline(false);
    if (evts)   setEvents(evts.events);
    setLastRefresh(new Date());
  }, []);

  const fetchSlow = useCallback(async () => {
    const [acct, rt, dr] = await Promise.all([
      api.account(),
      api.runtime(),
      api.dailyReport(),
    ]);
    if (acct && !acct.error) setAccount(acct);
    if (rt) setRuntime(rt);
    if (dr) setDailyReport(dr);
  }, []);

  const fetchHermes = useCallback(async () => {
    const h = await api.hermes();
    if (h) setHermes(h);
  }, []);

  const fetchEodReview = useCallback(async () => {
    const r = await api.eodReview();
    if (r) setEodReview(r);
  }, []);

  const refreshAll = useCallback(() => {
    fetchFast();
    fetchSlow();
    fetchHermes();
    fetchEodReview();
  }, [fetchFast, fetchSlow, fetchHermes, fetchEodReview]);

  useEffect(() => {
    refreshAll();
    fastTimer.current   = setInterval(fetchFast,      FAST_MS);
    slowTimer.current   = setInterval(fetchSlow,      SLOW_MS);
    hermesTimer.current = setInterval(fetchHermes,    HERMES_MS);
    eodTimer.current     = setInterval(fetchEodReview, EOD_MS);
    return () => {
      if (fastTimer.current)   clearInterval(fastTimer.current);
      if (slowTimer.current)   clearInterval(slowTimer.current);
      if (hermesTimer.current) clearInterval(hermesTimer.current);
      if (eodTimer.current)    clearInterval(eodTimer.current);
    };
  }, [fetchFast, fetchSlow, fetchHermes, fetchEodReview, refreshAll]);

  const positions    = dashboard?.open_positions ?? [];
  const recentEvents = events.length > 0 ? events : (dashboard?.recent_events ?? []);
  const watchlist    = runtime?.watchlist ?? [];

  return (
    <div
      className="min-h-screen px-4 py-4 md:px-6 md:py-5 space-y-4"
      style={{ background: "var(--surface)" }}
    >
      {/* ── Command Center Header ──────────────────────────────────── */}
      <StatusBar
        dashboard={dashboard}
        runtime={runtime}
        hermes={hermes}
        lastRefresh={lastRefresh}
        apiOnline={apiOnline}
        etTime={etTime}
        onRefresh={refreshAll}
      />

      {/* ── Market Status Banner ───────────────────────────────────── */}
      <MarketBanner dashboard={dashboard} runtime={runtime} />

      {/* ── KPI Cards ─────────────────────────────────────────────── */}
      <AccountCards account={account} dashboard={dashboard} hermes={hermes} dailyReport={dailyReport} />

      {/* ── Session Activity ───────────────────────────────────────── */}
      <SessionSummary runtime={runtime} hermes={hermes} dailyReport={dailyReport} />

      {/* ── Open Positions ─────────────────────────────────────────── */}
      <PositionsTable positions={positions} />

      {/* ── Watchlist ──────────────────────────────────────────────── */}
      {watchlist.length > 0 && (
        <WatchlistPanel watchlist={watchlist} events={recentEvents} />
      )}

      {/* ── Main 3-col grid: Timeline | Hermes | Risk ─────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Event Timeline spans 2 cols on large screens */}
        <div className="lg:col-span-2">
          <EventTimeline events={recentEvents} />
        </div>

        {/* Right column: Hermes + Risk stacked */}
        <div className="flex flex-col gap-4">
          <HermesPanel hermes={hermes} recentEvents={recentEvents} eodReview={eodReview} />
          <RiskPanel dashboard={dashboard} runtime={runtime} />
        </div>
      </div>

      {/* ── Footer ─────────────────────────────────────────────────── */}
      <div className="py-4 text-center text-[10px] text-[#364f63] tracking-wide">
        Mission Control v2 · Read-Only · No trading controls · Paper Mode Only
      </div>
    </div>
  );
}
