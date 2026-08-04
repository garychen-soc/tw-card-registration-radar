import { TAIPEI, parseAt } from "./derive";

const dayFmt = new Intl.DateTimeFormat("zh-TW", {
  timeZone: TAIPEI,
  month: "long",
  day: "numeric",
  weekday: "short",
});
const shortFmt = new Intl.DateTimeFormat("zh-TW", {
  timeZone: TAIPEI,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
const clockFmt = new Intl.DateTimeFormat("zh-TW", {
  timeZone: TAIPEI,
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function formatDay(value?: string): string {
  const at = parseAt(value);
  return at ? dayFmt.format(at) : "—";
}

export function formatDate(value?: string): string {
  const at = parseAt(value);
  return at ? shortFmt.format(at) : "未公告";
}

export function formatClock(value?: string): string {
  const at = parseAt(value);
  return at ? clockFmt.format(at) : "—";
}

export function formatMoment(value?: string): string {
  const at = parseAt(value);
  return at ? `${shortFmt.format(at)} ${clockFmt.format(at)}` : "—";
}

export function formatMoney(value?: number): string {
  return value === undefined ? "—" : `NT$${value.toLocaleString("zh-TW")}`;
}

export function formatPeriod(start?: string, end?: string): string {
  if (!start && !end) return "期間未公告";
  if (start && !end) return `${formatDate(start)} 起（未公告結束）`;
  if (!start && end) return `至 ${formatDate(end)}`;
  return `${formatDate(start)} – ${formatDate(end)}`;
}
