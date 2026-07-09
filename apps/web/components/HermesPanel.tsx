"use client";

import type { HermesSummary, BotEvent, Blocker, EndOfDayReview } from "@/lib/types";

interface Props {
  hermes: HermesSummary | null;
  recentEvents: BotEvent[];
  eodReview?: EndOfDayReview | null;
}

/* ── Horizontal bar chart for blockers ─────────────────────────────── */
function BlockerChart({ blockers }: { blockers: Blocker[] }) {
  if (blockers.length === 0) return null;
  const max = Math.max(...blockers.map(b => b.count), 1);

  return (
    <div className="space-y-2">
      {blockers.slice(0, 7).map(b => (
        <div key={b.blocker} className="flex items-center gap-2">
          <span
            className="w-32 text-[10px] text-slate-400 truncate shrink-0"
            title={b.blocker}
          >
            {b.blocker.replace(/_/g, " ")}
          </span>
          <div className="flex-1 h-1 bg-[#192c40] rounded-full overflow-hidden">
            <div
              className="h-full bg-amber-500/50 rounded-full transition-all duration-700"
              style={{ width: `${(b.count / max) * 100}%` }}
            />
          </div>
          <span className="text-[10px] text-amber-400 font-bold w-5 text-right shrink-0">
            {b.count}
          </span>
        </div>
      ))}
    </div>
  );
}

/* ── Section wrapper ────────────────────────────────────────────────── */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-[#364f63] mb-2">
        {title}
      </div>
      {children}
    </div>
  );
}

/* ── Find top candidate from scan events ─────────────────────────────── */
function findTopCandidate(events: BotEvent[]): BotEvent | null {
  return events
    .filter(e => e.event_type === "scan_cycle_completed" && e.score != null)
    .reduce<BotEvent | null>((best, e) =>
      best === null || (e.score ?? 0) > (best.score ?? 0) ? e : best,
      null
    );
}

/* ── Strategy verdict chip ─────────────────────────────────────────── */
function VerdictChip({ verdict }: { verdict: string }) {
  const isReview = verdict === "REVIEW_AFTER_MORE_DATA";
  return (
    <span
      className={`chip text-[9px] font-bold tracking-widest ${
        isReview ? "chip-amber" : "chip-green"
      }`}
    >
      {verdict.replace(/_/g, " ")}
    </span>
  );
}

export default function HermesPanel({ hermes, recentEvents, eodReview }: Props) {
  const topCandidate = findTopCandidate(recentEvents);
  const correct = hermes?.bot_behaved_correctly ?? null;

  return (
    <div className="panel flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="panel-title mb-0">Hermes · AI Analyst</div>
        {correct !== null && (
          <span
            className={`chip text-[9px] font-bold tracking-widest ${
              correct ? "chip-green" : "chip-red"
            }`}
          >
            {correct ? "✓ NOMINAL" : "✕ REVIEW"}
          </span>
        )}
      </div>

      {/* Bot correctness notes */}
      {hermes?.correctness_notes && hermes.correctness_notes.length > 0 && (
        <Section title="Correctness">
          <ul className="space-y-1">
            {hermes.correctness_notes.map((n, i) => (
              <li key={i} className="text-[11px] text-slate-300 flex gap-1.5">
                <span className="text-[#364f63] shrink-0">›</span>
                <span>{n}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {/* Top candidate */}
      <Section title="Top Candidate">
        {hermes?.best_near_miss ? (
          <div className="bg-amber-950/30 rounded-lg px-3 py-2 text-[11px] text-amber-300 ring-1 ring-amber-800/40">
            {hermes.best_near_miss}
          </div>
        ) : topCandidate ? (
          <div className="bg-[#0a1320] rounded-lg px-3 py-2 flex items-center gap-3 ring-1 ring-[#192c40]">
            <span className="font-bold text-slate-100 text-[13px]">{topCandidate.symbol}</span>
            {topCandidate.score != null && (
              <span className="text-amber-400 font-semibold text-[12px]">{topCandidate.score}</span>
            )}
            {topCandidate.grade && (
              <span className="chip chip-muted text-[10px]">{topCandidate.grade}</span>
            )}
            {topCandidate.blocked_by && (
              <span className="text-[10px] text-amber-500/70 truncate">
                blocked: {topCandidate.blocked_by.replace(/_/g, " ")}
              </span>
            )}
          </div>
        ) : (
          <div className="text-[11px] text-[#364f63]">No candidate data yet</div>
        )}
      </Section>

      {/* Main blockers chart */}
      {hermes?.main_blockers && hermes.main_blockers.length > 0 && (
        <Section title="Top Blockers">
          <BlockerChart blockers={hermes.main_blockers} />
        </Section>
      )}

      {/* Why no trades */}
      {hermes?.why_no_trades && (
        <Section title="Why No Trades">
          <div className="text-[11px] text-slate-400 leading-relaxed bg-[#0a1320] rounded-lg px-3 py-2.5 ring-1 ring-[#192c40]">
            {hermes.why_no_trades}
          </div>
        </Section>
      )}

      {/* What happened */}
      {hermes?.what_happened && hermes.what_happened.length > 0 && (
        <Section title="What Happened">
          <ul className="space-y-1">
            {hermes.what_happened.map((item, i) => (
              <li key={i} className="text-[11px] text-slate-300 flex gap-1.5">
                <span className="text-[#364f63] shrink-0">›</span>
                <span>{item}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {/* Warnings / errors */}
      {hermes?.warnings_errors && hermes.warnings_errors.length > 0 && (
        <Section title="Warnings & Errors">
          <div className="space-y-1.5">
            {hermes.warnings_errors.map((w, i) => (
              <div
                key={i}
                className={`text-[10px] rounded px-2.5 py-1.5 leading-relaxed ${
                  w.severity === "error"
                    ? "bg-red-950/40 text-red-300 ring-1 ring-red-900/60"
                    : "bg-amber-950/40 text-amber-300 ring-1 ring-amber-900/60"
                }`}
              >
                <span className="font-semibold">{w.type}: </span>{w.message}
                {w.time && <span className="ml-2 opacity-50">{w.time}</span>}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Needs review */}
      {hermes?.needs_review && hermes.needs_review.length > 0 && (
        <Section title="Needs Review">
          <div className="space-y-1">
            {hermes.needs_review.map((item, i) => (
              <div key={i} className="text-[11px] text-amber-300 flex gap-1.5">
                <span className="text-amber-600 shrink-0">⚠</span>
                {item}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* End-of-day review */}
      {eodReview && (
        <div className="pt-4 border-t border-[#192c40] space-y-5">
          <div className="flex items-center justify-between">
            <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-[#364f63]">
              End-of-Day Review
            </div>
            <span className="text-[9px] text-[#364f63]">{eodReview.session_date}</span>
          </div>

          {/* Executive summary */}
          <div className="text-[11px] text-slate-300 leading-relaxed bg-[#0a1320] rounded-lg px-3 py-2.5 ring-1 ring-[#192c40]">
            {eodReview.executive_summary}
          </div>

          {/* Best opportunity */}
          <Section title="Best Opportunity">
            {eodReview.best_opportunity ? (
              <div className="bg-[#0a1320] rounded-lg px-3 py-2 flex items-center gap-3 ring-1 ring-[#192c40]">
                <span className="font-bold text-slate-100 text-[13px]">
                  {eodReview.best_opportunity.symbol}
                </span>
                {eodReview.best_opportunity.score != null && (
                  <span className="text-amber-400 font-semibold text-[12px]">
                    {eodReview.best_opportunity.score}
                  </span>
                )}
                {eodReview.best_opportunity.grade && (
                  <span className="chip chip-muted text-[10px]">{eodReview.best_opportunity.grade}</span>
                )}
                {eodReview.best_opportunity.realized_pnl != null && (
                  <span className="text-[10px] text-slate-400">
                    P&amp;L ${eodReview.best_opportunity.realized_pnl.toFixed(2)}
                  </span>
                )}
              </div>
            ) : (
              <div className="text-[11px] text-[#364f63]">No standout opportunity today</div>
            )}
          </Section>

          {/* Top blockers */}
          {eodReview.blocker_breakdown.length > 0 && (
            <Section title="Top Blockers">
              <BlockerChart blockers={eodReview.blocker_breakdown} />
            </Section>
          )}

          {/* Lessons learned */}
          {eodReview.lessons_learned.length > 0 && (
            <Section title="Lessons Learned">
              <ul className="space-y-1">
                {eodReview.lessons_learned.map((item, i) => (
                  <li key={i} className="text-[11px] text-slate-300 flex gap-1.5">
                    <span className="text-[#364f63] shrink-0">›</span>
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {/* Recommended next steps */}
          {eodReview.recommended_next_steps.length > 0 && (
            <Section title="Recommended Next Steps">
              <ul className="space-y-1">
                {eodReview.recommended_next_steps.map((item, i) => (
                  <li key={i} className="text-[11px] text-slate-300 flex gap-1.5">
                    <span className="text-[#364f63] shrink-0">›</span>
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {/* Strategy verdict */}
          <Section title="Strategy Verdict">
            <div className="flex items-center gap-2.5">
              <VerdictChip verdict={eodReview.strategy_change_recommendation.verdict} />
              <span className="text-[10px] text-slate-500">
                {eodReview.strategy_change_recommendation.reason}
              </span>
            </div>
          </Section>
        </div>
      )}

      {/* Safety footer */}
      <div className="mt-auto pt-3 border-t border-[#192c40] text-[9px] text-[#364f63] leading-relaxed">
        {hermes?.safety_note ?? "READ-ONLY · Hermes cannot place trades or modify strategy"}
        {hermes?.session_date && (
          <span className="ml-3 text-[#364f63]">Session: {hermes.session_date}</span>
        )}
      </div>
    </div>
  );
}
