/**
 * 所有「今天」的函數都在這裡算。
 *
 * 資料檔刻意不含 lifecycle、是否進行中、今日待登錄之類的欄位 —— 那些是時間的
 * 函數，凍結在資料裡就會在資料放久之後顯示錯的狀態（前身的網站就是這樣，
 * 08-01 的資料在 08-04 開啟，8/3 開始的活動掛著「即將開始」）。
 */

import type { AgendaEntry, ContractKind, RegWindow } from "./types";

export const TAIPEI = "Asia/Taipei";

/** 台北時區的 YYYY-MM-DD。用 en-CA 是因為它的格式剛好就是 ISO 日期。 */
export function taipeiDay(at: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: TAIPEI,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(at);
}

export function addDays(day: string, days: number): string {
  const base = new Date(`${day}T00:00:00+08:00`);
  return taipeiDay(new Date(base.getTime() + days * 86400000));
}

export function parseAt(value?: string): Date | null {
  if (!value) return null;
  const text = /^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00+08:00` : value;
  const at = new Date(text);
  return Number.isNaN(at.getTime()) ? null : at;
}

/** 視窗的時間定位點。deadline 型只有截止時間。 */
export function anchorOf(window: RegWindow): Date | null {
  return parseAt(window.start) ?? parseAt(window.end);
}

export type WindowState = "open" | "upcoming" | "closed" | "unknown";

export function windowState(window: RegWindow, now: Date = new Date()): WindowState {
  const start = parseAt(window.start);
  const end = parseAt(window.end);
  if (window.kind === "deadline" && end) return end >= now ? "open" : "closed";
  if (start && start > now) return "upcoming";
  if (start && !end) return "open"; // 官方未公告截止 —— 只能說已開放
  if (start && end) return end >= now ? "open" : "closed";
  return "unknown";
}

export type Lifecycle = "active" | "upcoming" | "ended" | "unknown";

export function lifecycleOf(entry: AgendaEntry, today: string = taipeiDay()): Lifecycle {
  const start = entry.period?.start;
  const end = entry.period?.end;
  if (end && end < today) return "ended";
  if (start && start > today) return "upcoming";
  if (start || end) return "active";
  return "unknown";
}

/** 下一個還沒關閉的登錄時點。沒有就回 null。 */
export function nextWindow(entry: AgendaEntry, now: Date = new Date()): RegWindow | null {
  const upcoming = (entry.windows ?? [])
    .filter((w) => windowState(w, now) !== "closed")
    .sort((a, b) => (anchorOf(a)?.getTime() ?? 0) - (anchorOf(b)?.getTime() ?? 0));
  return upcoming[0] ?? null;
}

export function windowsOn(entry: AgendaEntry, day: string): RegWindow[] {
  return (entry.windows ?? []).filter((w) => {
    const anchor = anchorOf(w);
    return anchor !== null && taipeiDay(anchor) === day;
  });
}

/** 有效消費區間 —— 可消費期間與登錄時序的交集。雙軌時序帶要畫的重疊區。 */
export function spendWindow(
  entry: AgendaEntry,
  contractStart?: string,
): { start?: string; end?: string } {
  const start = entry.period?.start;
  if (entry.contract === "register_before_spend" && contractStart) {
    const from = taipeiDay(new Date(contractStart));
    return { start: !start || from > start ? from : start, end: entry.period?.end };
  }
  return { start, end: entry.period?.end };
}

export const CONTRACT_LABEL: Record<ContractKind, string> = {
  register_before_spend: "先登錄後消費",
  retroactive_ok: "可事後補登錄",
  registration_closes_early: "登錄先截止",
  per_period_reregister: "每期需重新登錄",
  unknown: "時序未確認",
};

export const CONTRACT_NOTE: Record<ContractKind, string> = {
  register_before_spend: "登錄前的消費不計入 —— 先刷就白刷",
  retroactive_ok: "活動結束後仍可登錄，風險較低",
  registration_closes_early: "錯過登錄期限就整檔拿不到，即使還在活動期間",
  per_period_reregister: "登錄一次不夠，每期都要重新登錄",
  unknown: "官方資訊不足，無法判斷登錄與消費的先後",
};

export const CONTRACT_RISK: Record<ContractKind, "high" | "medium" | "low" | "none"> = {
  register_before_spend: "high",
  registration_closes_early: "medium",
  per_period_reregister: "medium",
  retroactive_ok: "low",
  unknown: "none",
};

export const REVIEW_LABEL: Record<string, string> = {
  window_outside_period: "登錄時點落在活動期間之外，可能是本頁含多個活動而被合併",
  windows_overlap: "同一活動的登錄視窗互相重疊",
  registration_without_window: "標記為需登錄，但抓不到任何登錄時點",
  registration_end_unknown: "抓到登錄開放時間，但截止時間未確認",
  contract_unknown: "無法判斷登錄與消費的先後關係",
  threshold_not_monotonic: "階梯門檻的消費金額未遞增，可能解析錯位",
  period_missing: "抓不到活動期間",
  low_confidence_window: "登錄時點的解析信心不足",
  offer_boundary_missing: "本頁應含多個活動但未能切出邊界",
};

/** 資料新鮮度。超過 36 小時就在頁面上明說 —— 靜默過期是前身最大的信任問題。 */
export function stalenessHours(generatedAt: string, now: Date = new Date()): number {
  const at = parseAt(generatedAt);
  if (!at) return Number.POSITIVE_INFINITY;
  return (now.getTime() - at.getTime()) / 3600000;
}
