/**
 * 單筆活動的行事曆匯出。
 *
 * 為什麼卡片上要有這個：訂閱整份 feed 對「我只想記這一筆」的人太重，而且
 * Google 日曆的訂閱必須用瀏覽器加一次（Android 的 App 裡沒有這個功能）。
 * 逐筆加入則是每個平台都通的 —— Google 用 TEMPLATE 網址，其餘用 .ics 檔。
 */

import { formatMoment } from "./format";
import type { AgendaEntry, RegWindow } from "./types";
import { CONTRACT_LABEL, CONTRACT_NOTE, anchorOf } from "./derive";

/** Google 與 iCalendar 都要 UTC 的 YYYYMMDDTHHMMSSZ。 */
function stamp(at: Date): string {
  return `${at.toISOString().replace(/[-:]/g, "").slice(0, 15)}Z`;
}

/** 事件長度。登錄開放時間多半沒有公告截止，給 15 分鐘只是為了讓它在行事曆上
 *  有寬度 —— 資料層的 end 仍然是 null，兩者刻意分開。 */
const FALLBACK_MINUTES = 15;

function windowRange(window: RegWindow): { start: Date; end: Date } | null {
  const start = anchorOf(window);
  if (!start) return null;
  const raw = window.end ? new Date(window.end) : null;
  const end =
    raw && raw > start ? raw : new Date(start.getTime() + FALLBACK_MINUTES * 60000);
  return { start, end };
}

function describe(entry: AgendaEntry, bank: string): string {
  const lines = [
    `${bank}｜${entry.title}`,
    entry.page_title ? `活動頁：${entry.page_title}` : "",
    entry.contract ? `${CONTRACT_LABEL[entry.contract]}——${CONTRACT_NOTE[entry.contract]}` : "",
    entry.quota_limited ? `限量${entry.quota_seats ? ` ${entry.quota_seats}` : ""}，開放即需準時登錄` : "",
    entry.url ? `官方頁：${entry.url}` : "",
    "",
    "由「刷卡登錄雷達」產生。實際名額、資格與回饋一律以銀行官方公告為準。",
  ];
  return lines.filter(Boolean).join("\n");
}

/** Google 日曆的新增事件網址。Android 會直接開 App，桌機開網頁版。 */
export function googleCalendarUrl(
  entry: AgendaEntry,
  window: RegWindow,
  bank: string,
): string | null {
  const range = windowRange(window);
  if (!range) return null;
  const params = new URLSearchParams({
    action: "TEMPLATE",
    text: `登錄：${bank}${entry.title ? `｜${entry.title}` : ""}`.slice(0, 200),
    dates: `${stamp(range.start)}/${stamp(range.end)}`,
    details: describe(entry, bank),
    ctz: "Asia/Taipei",
  });
  return `https://calendar.google.com/calendar/render?${params}`;
}

/** RFC 5545 的 TEXT 逃脫。反斜線要先處理，否則會把後面補上的逃脫再逃脫一次。
 *  `;` 的逃脫寫成 "\\;" 而不是 "\;" —— 後者在 JS 字串裡等於 ";"，
 *  逃脫會靜默失效，直到某家銀行的活動名稱出現分號才會炸。 */
function escapeText(value: string): string {
  return value
    .replace(/\\/g, "\\\\")
    .replace(/;/g, "\\;")
    .replace(/,/g, "\\,")
    .replace(/\n/g, "\\n");
}

/** RFC 5545 規定每行最多 75 個八位元組，折行要以空白開頭續行。 */
function fold(line: string): string {
  const bytes = new TextEncoder().encode(line);
  if (bytes.length <= 75) return line;
  const out: string[] = [];
  let chunk = "";
  let size = 0;
  for (const ch of line) {
    const width = new TextEncoder().encode(ch).length;
    if (size + width > (out.length ? 74 : 75)) {
      out.push(chunk);
      chunk = "";
      size = 0;
    }
    chunk += ch;
    size += width;
  }
  if (chunk) out.push(chunk);
  return out.join("\r\n ");
}

/** 單一事件的 .ics 內容。Apple、Outlook、三星行事曆都吃這個。 */
export function singleEventIcs(
  entry: AgendaEntry,
  window: RegWindow,
  bank: string,
  now: Date = new Date(),
): string | null {
  const range = windowRange(window);
  if (!range) return null;
  const lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//TW Card Registration Radar//ZH-TW//EN",
    "CALSCALE:GREGORIAN",
    "BEGIN:VEVENT",
    `UID:${entry.id}-${stamp(range.start)}@tw-card-registration-radar`,
    `DTSTAMP:${stamp(now)}`,
    `DTSTART:${stamp(range.start)}`,
    `DTEND:${stamp(range.end)}`,
    `SUMMARY:${escapeText(`登錄：${bank}${entry.title ? `｜${entry.title}` : ""}`)}`,
    `DESCRIPTION:${escapeText(describe(entry, bank))}`,
    entry.url ? `URL:${entry.url}` : "",
    "BEGIN:VALARM",
    "TRIGGER:-PT15M",
    "ACTION:DISPLAY",
    `DESCRIPTION:${escapeText(`15 分鐘後開放登錄：${bank}`)}`,
    "END:VALARM",
    "END:VEVENT",
    "END:VCALENDAR",
  ];
  return lines.filter(Boolean).map(fold).join("\r\n");
}

/** 觸發下載。用 Blob 而不是 data: URI —— iOS Safari 對 data: 下載的支援不可靠。 */
export function downloadIcs(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/calendar;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 這筆活動要放進行事曆的登錄時點：下一個還沒關閉的；全關了就用最後一個。 */
export function calendarWindow(entry: AgendaEntry, now: Date = new Date()): RegWindow | null {
  const windows = entry.windows ?? [];
  if (!windows.length) return null;
  const upcoming = windows
    .filter((w) => {
      const at = anchorOf(w);
      return at !== null && at.getTime() >= now.getTime();
    })
    .sort((a, b) => (anchorOf(a)?.getTime() ?? 0) - (anchorOf(b)?.getTime() ?? 0));
  return upcoming[0] ?? windows[windows.length - 1];
}

export { formatMoment };
