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
  const upcoming = scheduleWindows(entry, now)
    .filter((w) => windowState(w, now) !== "closed")
    .sort((a, b) => (anchorOf(a)?.getTime() ?? 0) - (anchorOf(b)?.getTime() ?? 0));
  return upcoming[0] ?? null;
}

export function windowsOn(entry: AgendaEntry, day: string): RegWindow[] {
  // scheduleWindows 而不是 entry.windows —— 只有循環規則、沒有具體日期的活動
  // 否則永遠排不進時間軸（實測 127 筆）。
  return scheduleWindows(entry).filter((w) => {
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
  offer_boundary_missing: "本頁含多個活動且未能完全分開，請至官方頁確認對應的條件",
};

/** 資料新鮮度。超過 36 小時就在頁面上明說 —— 靜默過期是前身最大的信任問題。 */
export function stalenessHours(generatedAt: string, now: Date = new Date()): number {
  const at = parseAt(generatedAt);
  if (!at) return Number.POSITIVE_INFINITY;
  return (now.getTime() - at.getTime()) / 3600000;
}


/**
 * 循環登錄的下一次時點。
 *
 * 實測 127 筆活動只有「每月 N 日 HH:MM 開放登錄」這種規則、沒有任何具體日期，
 * 時間軸因此一次都不會提醒 —— 而它們正是「每個月都要重新登錄」這種最容易被
 * 忘記的類型。這裡在讀取端把規則展開成日期，資料層維持沒有時間衍生狀態。
 *
 * 只展開算得出日期的（每月第 N 個星期幾沒有固定日期，只會顯示 note）。
 * 超出活動期間的不展開 —— 活動都結束了還提醒登錄沒有意義。
 */
export function recurringWindows(entry: AgendaEntry, now: Date = new Date(), count = 2): RegWindow[] {
  const rule = entry.recurrence;
  if (!rule || rule.kind !== "monthly" || !rule.day_of_month) return [];
  const periodEnd = parseAt(entry.period?.end);
  const out: RegWindow[] = [];
  const cursor = new Date(now.getTime());
  for (let step = 0; step < 14 && out.length < count; step += 1) {
    const at = monthlyOccurrence(cursor, rule.day_of_month, rule.hour ?? 0, rule.minute ?? 0, step);
    if (!at) continue;
    if (at.getTime() < now.getTime()) continue;
    if (periodEnd && at.getTime() > periodEnd.getTime() + 86400000) break;
    out.push({ kind: "opens_at", start: at.toISOString(), derived: true });
  }
  return out;
}

/** 從 base 起算第 offset 個月的「N 日 HH:MM」（台北時間）。該月沒有這一天就跳過。 */
function monthlyOccurrence(
  base: Date,
  day: number,
  hour: number,
  minute: number,
  offset: number,
): Date | null {
  const taipeiNow = new Date(base.toLocaleString("en-US", { timeZone: TAIPEI }));
  const year = taipeiNow.getFullYear();
  const month = taipeiNow.getMonth() + offset;
  const probe = new Date(Date.UTC(year, month, 1));
  const daysInMonth = new Date(Date.UTC(probe.getUTCFullYear(), probe.getUTCMonth() + 1, 0)).getUTCDate();
  if (day > daysInMonth) return null;
  const stamp = `${probe.getUTCFullYear()}-${String(probe.getUTCMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}T${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}:00+08:00`;
  const at = new Date(stamp);
  return Number.isNaN(at.getTime()) ? null : at;
}

/**
 * 這筆活動在時間軸上的全部時點：官方明示的視窗 **加上** 循環規則推算出來的。
 *
 * 刻意不是「有明示視窗就不展開循環」。實測台北富邦 D000275：頁面同時寫了
 * 「每月17日16:00開放登錄」與一段七月的範例期間，我們把七月那段解析成視窗，
 * 於是循環規則永遠不展開 —— 使用者看到的是一個早就過期的七月時點，而真正
 * 每個月都會再開放的規則消失了。兩者都是官方寫的，兩者都要顯示。
 *
 * 已經有明示視窗覆蓋到的那一天不重複加，避免同一天出現兩列。
 */
export function scheduleWindows(entry: AgendaEntry, now: Date = new Date()): RegWindow[] {
  const explicit = entry.windows ?? [];
  const covered = new Set(
    explicit.map((w) => anchorOf(w)).filter((at): at is Date => at !== null).map(taipeiDay),
  );
  const derived = recurringWindows(entry, now).filter((w) => {
    const at = anchorOf(w);
    return at !== null && !covered.has(taipeiDay(at));
  });
  return [...explicit, ...derived];
}
