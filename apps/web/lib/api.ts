import type {
  DashboardData,
  Account,
  RuntimeStats,
  HermesSummary,
  EventsResponse,
  DailyReport,
  EndOfDayReview,
} from "./types";

const BASE = "/api";

async function get<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
    if (!res.ok) return null;
    return res.json() as Promise<T>;
  } catch {
    return null;
  }
}

export const api = {
  dashboard:   ()            => get<DashboardData>("/dashboard-data"),
  account:     ()            => get<Account>("/account"),
  runtime:     ()            => get<RuntimeStats>("/runtime-stats"),
  hermes:      ()            => get<HermesSummary>("/hermes/session-summary"),
  events:      (limit = 100) => get<EventsResponse>(`/events?limit=${limit}`),
  dailyReport: ()            => get<DailyReport>("/daily-report"),
  eodReview:   ()            => get<EndOfDayReview>("/hermes/end-of-day-review"),
};
