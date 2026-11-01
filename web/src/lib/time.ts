/** 台北時區的日期工具。瀏覽器可能不在台北，所有「今天」都以台北為準。 */

const TZ = "Asia/Taipei";
const WEEKDAY = ["日", "一", "二", "三", "四", "五", "六"];

const dayFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
});
const partsFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ, year: "numeric", month: "numeric", day: "numeric",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23", weekday: "short",
});

/** 台北日期字串 YYYY-MM-DD。 */
export function taipeiDay(at: Date | string): string {
  return dayFmt.format(typeof at === "string" ? new Date(at) : at);
}

export function addDays(day: string, n: number): string {
  const [y, m, d] = day.split("-").map(Number);
  const next = new Date(Date.UTC(y!, m! - 1, d! + n));
  return next.toISOString().slice(0, 10);
}

function parts(at: Date): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of partsFmt.formatToParts(at)) out[p.type] = p.value;
  return out;
}

export function clock(iso: string): string {
  const p = parts(new Date(iso));
  return `${p.hour}:${p.minute}`;
}

export function monthDay(iso: string): string {
  const p = parts(new Date(iso));
  return `${p.month}/${p.day}`;
}

export function weekdayOf(day: string): string {
  const [y, m, d] = day.split("-").map(Number);
  return WEEKDAY[new Date(Date.UTC(y!, m! - 1, d!)).getUTCDay()]!;
}

/** 「11/2（一）12:00」；只有日期精度時省略時刻。 */
export function moment(iso: string, precision: "minute" | "day" = "minute"): string {
  const day = taipeiDay(iso);
  const head = `${monthDay(iso)}（${weekdayOf(day)}）`;
  return precision === "day" ? head : `${head} ${clock(iso)}`;
}

export function dayLabel(day: string, today: string): string {
  if (day === today) return "今日";
  if (day === addDays(today, 1)) return "明日";
  const [, m, d] = day.split("-").map(Number);
  return `${m}/${d}（${weekdayOf(day)}）`;
}

export function relative(iso: string, now: Date = new Date()): string {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  void now;
  if (minutes < 60) return `${Math.max(minutes, 1)} 分鐘前`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} 小時前`;
  return `${Math.round(hours / 24)} 天前`;
}
