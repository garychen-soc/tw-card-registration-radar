/**
 * 刷卡登錄雷達 v2 前端。
 *
 * 首屏只載 data/index.json（時間軸）；活動目錄、需人工確認在切換分頁時才載入
 * data/banks/*.json。「今天」一律以台北時間計算；資料檔裡沒有任何隨時間變動
 * 的狀態，放三天也不會顯示錯的「登錄中」。
 */

import type { Activity, BankData, IndexData, SubActivity, TimelineRow } from "./types";
import { addDays, clock, dayLabel, moment, relative, taipeiDay } from "./time";

const BASE = (document.body.dataset.base ?? "/").replace(/\/?$/, "/");
const SITE = new URL(BASE, location.origin).href;
const ICS_URL = `${SITE}calendar/registration.ics`;
const STORE_DONE = "radar.v2.done";
const STORE_BANKS = "radar.v2.banks";
const STALE_HOURS = 30;
const RECENT_OPEN_DAYS = 7;

interface State {
  index: IndexData | null;
  banks: Map<string, Activity[]>;
  tab: "timeline" | "catalog" | "review" | "sources";
  query: string;
  bank: string;
  mine: boolean;
  hideDone: boolean;
  regOnly: boolean;
}

const state: State = {
  index: null,
  banks: new Map(),
  tab: "timeline",
  query: "",
  bank: "",
  mine: false,
  hideDone: false,
  regOnly: true,
};

// ---- 小工具 ----------------------------------------------------------------

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K, cls = "", text = "",
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text) node.textContent = text;
  return node;
}

function link(href: string, text: string, cls = "btn"): HTMLAnchorElement {
  const a = el("a", cls, text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function save(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* 無痕模式等情況下寫不進去，不影響閱讀 */
  }
}

const done = new Set<string>(load<string[]>(STORE_DONE, []));
const myBanks = new Set<string>(load<string[]>(STORE_BANKS, []));

function doneKey(row: TimelineRow): string {
  return `${row.aid}|${row.sub_index}|${row.start ?? row.end}`;
}

// ---- 篩選 ----------------------------------------------------------------

function matchesText(...fields: string[]): boolean {
  const q = state.query.trim().toLowerCase();
  if (!q) return true;
  return fields.some((f) => f.toLowerCase().includes(q));
}

function matchesBank(bankId: string): boolean {
  if (state.bank && state.bank !== bankId) return false;
  if (state.mine && myBanks.size && !myBanks.has(bankId)) return false;
  return true;
}

function rowVisible(row: TimelineRow): boolean {
  if (!matchesBank(row.bank_id)) return false;
  if (state.hideDone && done.has(doneKey(row))) return false;
  return matchesText(row.title, row.sub, row.bank, row.quote);
}

// ---- 時間軸卡片 ------------------------------------------------------------

const REREG = { each_period: "每期需重新登錄", once: "登錄一次即可", unknown: "" } as const;
const ORDER = {
  register_before_spend: "須先登錄再消費",
  retroactive_ok: "活動期間內補登錄也算",
  unknown: "",
} as const;

function badges(row: { quota: number | null; reregister: keyof typeof REREG; order: keyof typeof ORDER }, shifted = false): HTMLElement {
  const box = el("div", "badges");
  if (row.quota) box.append(el("span", "badge hot", `限量 ${row.quota.toLocaleString("zh-TW")} 名`));
  if (REREG[row.reregister]) box.append(el("span", "badge", REREG[row.reregister]));
  if (ORDER[row.order]) {
    box.append(el("span", row.order === "register_before_spend" ? "badge warn" : "badge", ORDER[row.order]));
  }
  if (shifted) box.append(el("span", "badge", "已套用假日順延"));
  return box;
}

function googleUrl(row: TimelineRow): string | null {
  const startIso = row.start ?? row.end;
  if (!startIso) return null;
  const start = new Date(startIso);
  const end = new Date(start.getTime() + 15 * 60000);
  const z = (d: Date) => `${d.toISOString().replace(/[-:]/g, "").slice(0, 15)}Z`;
  const params = new URLSearchParams({
    action: "TEMPLATE",
    text: `登錄｜${row.bank}｜${row.title}${row.sub ? `｜${row.sub}` : ""}`.slice(0, 200),
    dates: `${z(start)}/${z(end)}`,
    details: [`原文：${row.quote}`, row.note, `活動頁：${row.url}`, `登錄：${row.portal}`]
      .filter(Boolean).join("\n"),
    ctz: "Asia/Taipei",
  });
  return `https://calendar.google.com/calendar/render?${params}`;
}

function slotCard(row: TimelineRow, mode: "opens" | "closes" | "open"): HTMLElement {
  const card = el("article", "slot");
  if (done.has(doneKey(row))) card.classList.add("is-done");
  if (row.stale) card.classList.add("is-stale");

  const when = el("div", "when");
  if (mode === "closes" && row.end) {
    when.append(el("strong", "", clock(row.end)), el("span", "tag end", "截止"));
  } else if (mode === "open") {
    when.append(el("span", "tag live", "登錄中"));
  } else if (row.start) {
    when.append(el("strong", "", row.precision === "day" ? "全天" : clock(row.start)));
    when.append(el("span", "tag", "開放"));
  }
  card.append(when);

  const body = el("div", "body");
  const head = el("h3");
  head.append(el("span", "bank", row.bank), document.createTextNode(row.title));
  body.append(head);
  if (row.sub) body.append(el("p", "sub", `子活動：${row.sub}`));

  const span = el("p", "span");
  if (row.start && row.end) {
    span.textContent = `${moment(row.start, row.precision)} → ${moment(row.end, row.precision)}`;
  } else if (row.start) {
    span.textContent = `${moment(row.start, row.precision)} 起（截止未公告，額滿為止）`;
  } else if (row.end) {
    span.textContent = `${moment(row.end)} 截止`;
  }
  body.append(span);
  body.append(badges(row, row.shifted));
  if (row.note) body.append(el("p", "note", row.note));
  if (row.stale) body.append(el("p", "note warn", "此來源本輪讀取失敗，顯示上一輪資料"));

  const evidence = el("details", "evidence");
  evidence.append(el("summary", "", "官方原文"));
  evidence.append(el("blockquote", "", row.quote));
  body.append(evidence);

  const actions = el("div", "actions");
  actions.append(link(row.portal || row.url, "前往登錄", "btn primary"));
  if (row.portal && row.portal !== row.url) actions.append(link(row.url, "活動頁"));
  const google = googleUrl(row);
  if (google) actions.append(link(google, "加到 Google 日曆"));
  const label = el("label", "check");
  const box = el("input");
  box.type = "checkbox";
  box.checked = done.has(doneKey(row));
  box.addEventListener("change", () => {
    if (box.checked) done.add(doneKey(row));
    else done.delete(doneKey(row));
    save(STORE_DONE, [...done]);
    card.classList.toggle("is-done", box.checked);
  });
  label.append(box, document.createTextNode("已登錄"));
  actions.append(label);
  body.append(actions);
  if (row.portal && row.portal !== row.url) {
    body.append(el("p", "hint", `${row.portal_hint || "統一登錄頁"}：到站後請找「${row.title}」`));
  }
  card.append(body);
  return card;
}

// ---- 分頁：時間軸 ----------------------------------------------------------

function renderTimeline(root: HTMLElement, data: IndexData): void {
  const now = new Date();
  const today = taipeiDay(now);
  const rows = data.timeline.filter(rowVisible);

  const open = rows.filter((r) => {
    if (!r.start) return false;
    const start = new Date(r.start);
    if (start > now || taipeiDay(start) === today) return false;
    if (r.end) return new Date(r.end) >= now;
    return now.getTime() - start.getTime() < RECENT_OPEN_DAYS * 86400000;
  });
  open.sort((a, b) => (a.end ?? "9999").localeCompare(b.end ?? "9999"));

  let shown = 0;
  for (let offset = 0; offset < 7; offset += 1) {
    const day = addDays(today, offset);
    const opens = rows.filter((r) => r.start && taipeiDay(r.start) === day);
    const closes = rows.filter(
      (r) => r.end && taipeiDay(r.end) === day && (!r.start || taipeiDay(r.start) !== day)
        && (offset > 0 || new Date(r.end) >= now),
    );
    if (!opens.length && !closes.length) continue;
    const section = el("section", "day");
    const head = el("h2");
    head.append(el("span", "day-label", dayLabel(day, today)));
    head.append(el("span", "day-count", `${opens.length} 個開放 · ${closes.length} 個截止`));
    section.append(head);
    const items = [
      ...opens.map((r) => ({ r, mode: "opens" as const, at: r.start! })),
      ...closes.map((r) => ({ r, mode: "closes" as const, at: r.end! })),
    ].sort((a, b) => a.at.localeCompare(b.at));
    for (const item of items) section.append(slotCard(item.r, item.mode));
    root.append(section);
    shown += items.length;
  }
  if (!shown) {
    const empty = el("p", "empty", "未來 7 天沒有符合條件的登錄時點。");
    root.append(empty);
  }

  if (open.length) {
    const box = el("details", "open-now");
    const summary = el("summary");
    summary.append(el("strong", "", `目前登錄中 ${open.length} 檔`),
      el("span", "muted", "（已開放、尚未截止；依截止時間排序）"));
    box.append(summary);
    for (const r of open) box.append(slotCard(r, "open"));
    root.append(box);
  }

  const later = rows.filter((r) => r.start && taipeiDay(r.start) > addDays(today, 6));
  if (later.length) {
    const box = el("details", "later");
    box.append(el("summary", "", `7 天後還有 ${later.length} 個登錄時點`));
    for (const r of later.slice(0, 200)) {
      const line = el("p", "later-row");
      line.append(el("span", "when", moment(r.start!, r.precision)),
        el("span", "bank", r.bank), document.createTextNode(` ${r.title}${r.sub ? `｜${r.sub}` : ""}`));
      box.append(line);
    }
    root.append(box);
  }
}

// ---- 分頁：活動目錄 / 需人工確認 --------------------------------------------

async function loadBanks(data: IndexData): Promise<void> {
  const pending = data.sources.filter((s) => !state.banks.has(s.bank_id));
  await Promise.all(pending.map(async (s) => {
    const res = await fetch(`${BASE}data/banks/${s.bank_id}.json`, { cache: "no-cache" });
    if (!res.ok) return;
    const body = (await res.json()) as BankData;
    state.banks.set(s.bank_id, body.activities);
  }));
}

function registrationLine(a: Activity): string {
  const slots = a.subs.reduce((n, s) => n + s.slots.length, 0);
  if (a.registration === "required") {
    return slots ? `需登錄 · ${slots} 個時點` : "需登錄 · 官方未寫具體時點，請看活動頁";
  }
  if (a.registration === "not_required") return "不需登錄";
  return "是否需登錄未確認";
}

function subBlock(sub: SubActivity): HTMLElement {
  const box = el("div", "subblock");
  if (sub.name) box.append(el("h4", "", sub.name));
  box.append(badges(sub));
  const now = Date.now();
  const upcoming = sub.slots.filter((s) => new Date(s.end ?? s.start ?? 0).getTime() >= now);
  for (const s of upcoming.slice(0, 6)) {
    const text = s.start && s.end
      ? `${moment(s.start, s.precision)} → ${moment(s.end, s.precision)}`
      : s.start ? `${moment(s.start, s.precision)} 起` : `${moment(s.end!)} 截止`;
    box.append(el("p", "slotline", text + (s.note ? `｜${s.note}` : "")));
  }
  for (const rule of sub.rules) box.append(el("blockquote", "", rule));
  for (const reason of sub.review) box.append(el("p", "note warn", reason));
  return box;
}

function activityCard(a: Activity): HTMLElement {
  const card = el("details", "activity");
  if (a.stale) card.classList.add("is-stale");
  const summary = el("summary");
  const head = el("div", "a-head");
  head.append(el("span", "bank", a.bank_name), el("strong", "", a.title));
  const meta = el("div", "a-meta");
  const period = a.period_start || a.period_end
    ? `${a.period_start ?? "即日起"} ～ ${a.period_end ?? "未公告"}`
    : "活動期間未公告";
  meta.append(el("span", "", period), el("span", a.registration === "required" ? "reg" : "", registrationLine(a)));
  summary.append(head, meta);
  card.append(summary);
  const body = el("div", "a-body");
  if (a.summary) body.append(el("p", "", a.summary));
  if (a.aliases.length) body.append(el("p", "muted", `也叫：${a.aliases.join("、")}`));
  if (a.registration_quote) body.append(el("blockquote", "", a.registration_quote));
  for (const sub of a.subs) if (sub.slots.length || sub.rules.length) body.append(subBlock(sub));
  for (const reason of a.review) body.append(el("p", "note warn", reason));
  const actions = el("div", "actions");
  actions.append(link(a.url, "官方活動頁", "btn primary"));
  if (a.registration === "required" && a.portal_url && a.portal_url !== a.url) {
    actions.append(link(a.portal_url, "前往登錄"));
  }
  body.append(actions);
  card.append(body);
  return card;
}

function activityVisible(a: Activity): boolean {
  if (!matchesBank(a.bank_id)) return false;
  return matchesText(a.title, a.bank_name, a.summary, ...a.aliases, ...a.subs.map((s) => s.name));
}

function renderList(root: HTMLElement, review: boolean): void {
  const all = [...state.banks.values()].flat().filter(activityVisible);
  const today = taipeiDay(new Date());
  let list = all.filter((a) => !a.period_end || a.period_end >= today);
  if (review) {
    list = list.filter((a) => a.review.length || a.subs.some((s) => s.review.length));
  } else if (state.regOnly) {
    list = list.filter((a) => a.registration === "required");
  }
  list.sort((a, b) => (a.period_end ?? "9999").localeCompare(b.period_end ?? "9999"));
  root.append(el("p", "muted", `${list.length} 筆活動${review ? "需要人工確認（附原因）" : ""}`));
  for (const a of list.slice(0, 400)) root.append(activityCard(a));
  if (list.length > 400) root.append(el("p", "muted", `僅顯示前 400 筆，請用搜尋或銀行篩選縮小範圍。`));
}

// ---- 分頁：來源狀態 --------------------------------------------------------

const STATUS = { ok: "正常", partial: "部分讀取", failed: "讀取失敗", blocked: "被擋" } as const;

function renderSources(root: HTMLElement, data: IndexData): void {
  const table = el("table", "sources");
  const head = el("tr");
  for (const h of ["銀行", "狀態", "活動數", "說明"]) head.append(el("th", "", h));
  table.append(head);
  for (const s of data.sources) {
    const tr = el("tr", `st-${s.status}`);
    const name = el("td");
    name.append(link(s.entry_url, s.bank_name, ""));
    const status = STATUS[s.status] + (s.status === "ok" && s.detail_failures ? "（有缺頁）" : "");
    tr.append(name, el("td", "", status), el("td", "", String(s.activities)),
      el("td", "", [s.message, s.carried ? `沿用上一輪 ${s.carried} 筆` : ""].filter(Boolean).join("；")));
    table.append(tr);
  }
  root.append(table);
}

// ---- 外框 ----------------------------------------------------------------

function renderHeader(data: IndexData): void {
  const updated = document.getElementById("updated")!;
  const age = (Date.now() - new Date(data.generated_at).getTime()) / 3600000;
  updated.textContent = `資料更新：${moment(data.generated_at)}（${relative(data.generated_at)}）`;
  const warn = document.getElementById("staleness")!;
  const bad = data.sources.filter((s) => s.status !== "ok");
  const notes: string[] = [];
  if (age > STALE_HOURS) notes.push(`資料已超過 ${Math.floor(age)} 小時未更新，時點可能已變動`);
  if (bad.length) notes.push(`${bad.map((s) => s.bank_name).join("、")} 本輪未完整讀取`);
  warn.hidden = !notes.length;
  warn.textContent = notes.join("；");

  const today = taipeiDay(new Date());
  const todayCount = data.timeline.filter((r) => r.start && taipeiDay(r.start) === today).length;
  const weekCount = data.timeline.filter(
    (r) => r.start && taipeiDay(r.start) >= today && taipeiDay(r.start) <= addDays(today, 6),
  ).length;
  const stats = document.getElementById("stats")!;
  stats.replaceChildren();
  for (const [n, label] of [
    [todayCount, "今日開放登錄"],
    [weekCount, "7 天內開放"],
    [data.counts.registration_required, "需登錄活動"],
    [data.counts.with_slots, "已確認時點"],
  ] as const) {
    const box = el("div", "stat");
    box.append(el("strong", "", n.toLocaleString("zh-TW")), el("span", "", label));
    stats.append(box);
  }

  const select = document.getElementById("bank") as HTMLSelectElement;
  if (select.options.length <= 1) {
    for (const s of data.sources) {
      const opt = el("option", "", s.bank_name);
      opt.value = s.bank_id;
      select.append(opt);
    }
  }
}

function renderMyBanks(data: IndexData): void {
  const box = document.getElementById("mybanks-list")!;
  box.replaceChildren();
  for (const s of data.sources) {
    const label = el("label", "check");
    const input = el("input");
    input.type = "checkbox";
    input.checked = myBanks.has(s.bank_id);
    input.addEventListener("change", () => {
      if (input.checked) myBanks.add(s.bank_id);
      else myBanks.delete(s.bank_id);
      save(STORE_BANKS, [...myBanks]);
      void render();
    });
    label.append(input, document.createTextNode(s.bank_name));
    box.append(label);
  }
}

async function render(): Promise<void> {
  const data = state.index;
  if (!data) return;
  const root = document.getElementById("view")!;
  root.replaceChildren();
  for (const tab of document.querySelectorAll<HTMLButtonElement>("[data-tab]")) {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === state.tab));
  }
  document.getElementById("reg-only-wrap")!.hidden = state.tab !== "catalog";
  document.getElementById("hide-done-wrap")!.hidden = state.tab !== "timeline";
  if (state.tab === "timeline") return renderTimeline(root, data);
  if (state.tab === "sources") return renderSources(root, data);
  if (state.banks.size < data.sources.length) {
    root.append(el("p", "muted", "載入活動資料中…"));
    await loadBanks(data);
    root.replaceChildren();
  }
  renderList(root, state.tab === "review");
}

function wire(): void {
  for (const tab of document.querySelectorAll<HTMLButtonElement>("[data-tab]")) {
    tab.addEventListener("click", () => {
      state.tab = tab.dataset.tab as State["tab"];
      void render();
    });
  }
  const q = document.getElementById("q") as HTMLInputElement;
  let timer = 0;
  q.addEventListener("input", () => {
    clearTimeout(timer);
    timer = window.setTimeout(() => { state.query = q.value; void render(); }, 150);
  });
  const bank = document.getElementById("bank") as HTMLSelectElement;
  bank.addEventListener("change", () => { state.bank = bank.value; void render(); });
  for (const [id, key] of [["mine", "mine"], ["hide-done", "hideDone"], ["reg-only", "regOnly"]] as const) {
    const input = document.getElementById(id) as HTMLInputElement;
    input.checked = state[key];
    input.addEventListener("change", () => { state[key] = input.checked; void render(); });
  }
  (document.getElementById("sub-apple") as HTMLAnchorElement).href = ICS_URL.replace(/^https?:/, "webcal:");
  (document.getElementById("sub-google") as HTMLAnchorElement).href =
    `https://calendar.google.com/calendar/r?cid=${encodeURIComponent(ICS_URL.replace(/^https?:/, "webcal:"))}`;
  (document.getElementById("sub-url") as HTMLInputElement).value = ICS_URL;
  document.getElementById("sub-copy")!.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(ICS_URL);
      document.getElementById("sub-copy")!.textContent = "已複製";
    } catch {
      (document.getElementById("sub-url") as HTMLInputElement).select();
    }
  });
}

async function boot(): Promise<void> {
  wire();
  const root = document.getElementById("view")!;
  try {
    const res = await fetch(`${BASE}data/index.json`, { cache: "no-cache" });
    if (!res.ok) throw new Error(String(res.status));
    state.index = (await res.json()) as IndexData;
  } catch (error) {
    root.replaceChildren(el("p", "empty", `資料載入失敗（${String(error)}），請稍後重新整理。`));
    return;
  }
  renderHeader(state.index);
  renderMyBanks(state.index);
  await render();
  // 跨日或久開分頁時重算「今日」
  window.setInterval(() => {
    if (!state.index) return;
    renderHeader(state.index);
    if (state.tab === "timeline") void render();
  }, 5 * 60000);
}

void boot();
