/**
 * 前端主程式。無框架、無 client 依賴。
 *
 * 三個設計重點：
 * 1. 首屏只讀 index.json（92KB）。條件細節在使用者展開時才按銀行載入 catalog。
 * 2. 所有時間狀態都在這裡算，不信任資料裡的任何「現在」。
 * 3. 篩選狀態同步到 URL —— 可分享、可書籤、上一頁會回到前一個篩選。
 */

import {
  CONTRACT_LABEL,
  CONTRACT_NOTE,
  CONTRACT_RISK,
  REVIEW_LABEL,
  addDays,
  anchorOf,
  lifecycleOf,
  nextWindow,
  parseAt,
  stalenessHours,
  taipeiDay,
  windowState,
  windowsOn,
} from "./derive";
import { formatClock, formatDate, formatDay, formatMoment, formatMoney, formatPeriod } from "./format";
import type { AgendaEntry, CatalogOffer, CatalogPayload, IndexPayload, RegWindow } from "./types";

const PAGE_SIZE = 50;
const STALE_HOURS = 36;
const MY_BANKS_KEY = "radar.myBanks";
const DONE_KEY = "radar.registered";

type Tab = "timeline" | "catalog" | "review" | "sources";

interface State {
  data: IndexPayload | null;
  tab: Tab;
  bank: string;
  need: "all" | "registration" | "quota" | "before-spend";
  query: string;
  page: number;
  myBanksOnly: boolean;
}

const state: State = {
  data: null,
  tab: "timeline",
  bank: "",
  need: "all",
  query: "",
  page: 1,
  myBanksOnly: false,
};

const catalogCache = new Map<string, Promise<CatalogPayload>>();
/** 完整目錄（全部 815 筆）。index.json 的 agenda 只含有登錄時點的那些，
 *  瀏覽目錄時需要全部 —— 按需一次載入 17 個 catalog 檔（約 490KB）。 */
let allOffers: AgendaEntry[] | null = null;
const offerDetails = new Map<string, CatalogOffer>();

function base(): string {
  const path = document.body.dataset.base ?? "";
  return path.endsWith("/") ? path.slice(0, -1) : path;
}

function readSet(key: string): Set<string> {
  try {
    const raw = localStorage.getItem(key);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

function writeSet(key: string, value: Set<string>): void {
  try {
    localStorage.setItem(key, JSON.stringify([...value]));
  } catch {
    /* 隱私模式下不可寫，忽略 */
  }
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function bankName(bankId: string): string {
  return state.data?.sources.find((s) => s.bank_id === bankId)?.bank_name ?? bankId;
}

function portalOf(bankId: string) {
  return state.data?.sources.find((s) => s.bank_id === bankId)?.portal;
}

// ── URL 狀態 ────────────────────────────────────────────

function readUrl(): void {
  const params = new URLSearchParams(location.search);
  const tab = params.get("tab");
  if (tab === "timeline" || tab === "catalog" || tab === "review" || tab === "sources") {
    state.tab = tab;
  }
  state.bank = params.get("bank") ?? "";
  const need = params.get("need");
  if (need === "registration" || need === "quota" || need === "before-spend") state.need = need;
  state.query = params.get("q") ?? "";
  state.page = Math.max(1, Number(params.get("page") ?? "1") || 1);
  state.myBanksOnly = params.get("mine") === "1";
}

function writeUrl(replace = false): void {
  const params = new URLSearchParams();
  if (state.tab !== "timeline") params.set("tab", state.tab);
  if (state.bank) params.set("bank", state.bank);
  if (state.need !== "all") params.set("need", state.need);
  if (state.query) params.set("q", state.query);
  if (state.page > 1) params.set("page", String(state.page));
  if (state.myBanksOnly) params.set("mine", "1");
  const url = `${location.pathname}${params.toString() ? `?${params}` : ""}`;
  if (replace) history.replaceState(null, "", url);
  else history.pushState(null, "", url);
}

// ── 篩選 ────────────────────────────────────────────────

function matches(entry: AgendaEntry, offers: AgendaEntry[]): boolean {
  void offers;
  if (state.bank && entry.bank_id !== state.bank) return false;
  if (state.myBanksOnly) {
    const mine = readSet(MY_BANKS_KEY);
    if (mine.size && !mine.has(entry.bank_id)) return false;
  }
  if (state.need === "registration" && !(entry.windows ?? []).length) return false;
  if (state.need === "quota" && !entry.quota_limited) return false;
  if (state.need === "before-spend" && entry.contract !== "register_before_spend") return false;
  if (state.query) {
    const needle = state.query.toLocaleLowerCase("zh-Hant");
    const hay = `${entry.title} ${bankName(entry.bank_id)}`.toLocaleLowerCase("zh-Hant");
    if (!hay.includes(needle)) return false;
  }
  return true;
}

// ── 元件 ────────────────────────────────────────────────

function riskBadge(entry: AgendaEntry): HTMLElement | null {
  const kind = entry.contract ?? "unknown";
  const risk = CONTRACT_RISK[kind];
  if (risk === "none") return null;
  const badge = el("span", `badge risk-${risk}`, CONTRACT_LABEL[kind]);
  badge.title = CONTRACT_NOTE[kind];
  return badge;
}

/**
 * 雙軌時序帶：上軌是可消費期間、下軌是可登錄期間，共用同一條時間軸。
 * 兩軌的重疊區就是「刷了會被計入」的那一段 —— 這是整個產品要回答的問題。
 */
function timingBar(entry: AgendaEntry): HTMLElement | null {
  const periodStart = parseAt(entry.period?.start);
  const periodEnd = parseAt(entry.period?.end);
  const windows = entry.windows ?? [];
  if (!periodStart && !windows.length) return null;

  const points: number[] = [];
  if (periodStart) points.push(periodStart.getTime());
  if (periodEnd) points.push(periodEnd.getTime());
  for (const w of windows) {
    const a = parseAt(w.start);
    const b = parseAt(w.end);
    if (a) points.push(a.getTime());
    if (b) points.push(b.getTime());
  }
  if (points.length < 2) return null;

  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const pct = (value: number) => ((value - min) / span) * 100;

  const wrap = el("div", "timing");
  const legend = el("div", "timing-legend");
  legend.append(el("span", "swatch spend"), el("span", "", "可消費"));
  legend.append(el("span", "swatch reg"), el("span", "", "可登錄"));
  wrap.append(legend);

  const spendTrack = el("div", "track");
  if (periodStart) {
    const bar = el("div", "bar spend");
    bar.style.left = `${pct(periodStart.getTime())}%`;
    bar.style.width = `${Math.max(2, pct(periodEnd?.getTime() ?? max) - pct(periodStart.getTime()))}%`;
    spendTrack.append(bar);
  }
  wrap.append(spendTrack);

  const regTrack = el("div", "track");
  for (const w of windows) {
    const a = parseAt(w.start) ?? parseAt(w.end);
    if (!a) continue;
    const b = parseAt(w.end);
    const bar = el("div", `bar reg ${w.end ? "" : "open-ended"}`.trim());
    bar.style.left = `${pct(a.getTime())}%`;
    bar.style.width = `${Math.max(1.5, pct(b?.getTime() ?? a.getTime()) - pct(a.getTime()))}%`;
    bar.title = w.end
      ? `登錄 ${formatMoment(w.start)} → ${formatMoment(w.end)}`
      : `${formatMoment(w.start ?? w.end)} 開放登錄（截止時間未公告）`;
    regTrack.append(bar);
  }
  wrap.append(regTrack);

  const scale = el("div", "timing-scale");
  scale.append(el("span", "", formatDate(new Date(min).toISOString())));
  scale.append(el("span", "", formatDate(new Date(max).toISOString())));
  wrap.append(scale);
  return wrap;
}

function windowLine(w: RegWindow): HTMLElement {
  const line = el("div", "window");
  const stateLabel = { open: "登錄中", upcoming: "即將開放", closed: "已結束", unknown: "未確認" }[
    windowState(w)
  ];
  const when =
    w.kind === "deadline"
      ? `登錄截止 ${formatMoment(w.end)}`
      : w.end
        ? `${formatMoment(w.start)} → ${formatMoment(w.end)}`
        : `${formatMoment(w.start)} 起（截止未公告）`;
  line.append(el("span", `pill state-${windowState(w)}`, stateLabel));
  line.append(el("span", "when", when));
  return line;
}

function registeredToggle(entry: AgendaEntry): HTMLElement {
  const done = readSet(DONE_KEY);
  const label = el("label", "done");
  const box = el("input");
  box.type = "checkbox";
  box.checked = done.has(entry.id);
  box.setAttribute("aria-label", `標記「${entry.title}」已登錄`);
  box.addEventListener("change", () => {
    const set = readSet(DONE_KEY);
    if (box.checked) set.add(entry.id);
    else set.delete(entry.id);
    writeSet(DONE_KEY, set);
    label.classList.toggle("is-done", box.checked);
  });
  label.classList.toggle("is-done", box.checked);
  label.append(box, el("span", "", "已登錄"));
  return label;
}

function offerCard(entry: AgendaEntry, options: { compact?: boolean } = {}): HTMLElement {
  const card = el("article", "card");
  if (entry.needs_review) card.classList.add("is-review");

  const head = el("header", "card-head");
  head.append(el("span", "bank", bankName(entry.bank_id)));
  const badges = el("div", "badges");
  const risk = riskBadge(entry);
  if (risk) badges.append(risk);
  if (entry.quota_limited) {
    const seats = entry.quota_seats ? `限量 ${entry.quota_seats}` : "限量";
    const badge = el("span", "badge quota", seats);
    badge.title = "名額有限，開放即需準時登錄";
    badges.append(badge);
  }
  if (entry.recurrence) badges.append(el("span", "badge recur", "每期重登"));
  const life = lifecycleOf(entry);
  if (life === "upcoming") badges.append(el("span", "badge soon", "即將開始"));
  if (life === "ended") badges.append(el("span", "badge ended", "已結束"));
  head.append(badges);
  card.append(head);

  const title = el("h3", "card-title");
  const link = el("a");
  link.href = entry.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = entry.title || "（無標題）";
  title.append(link);
  card.append(title);

  card.append(el("p", "period", formatPeriod(entry.period?.start, entry.period?.end)));

  const windows = entry.windows ?? [];
  if (windows.length) {
    const box = el("div", "windows");
    for (const w of windows.slice(0, 3)) box.append(windowLine(w));
    if (windows.length > 3) {
      box.append(el("p", "more", `另有 ${windows.length - 3} 個登錄時點`));
    }
    card.append(box);
  }

  if (!options.compact) {
    const bar = timingBar(entry);
    if (bar) card.append(bar);
  }

  if (entry.needs_review) {
    const why = el("div", "review-why");
    why.append(el("strong", "", "需人工確認"));
    for (const code of entry.review_codes ?? []) {
      why.append(el("p", "", REVIEW_LABEL[code] ?? code));
    }
    card.append(why);
  }

  const foot = el("footer", "card-foot");
  const portal = portalOf(entry.bank_id);
  if (portal?.url) {
    const go = el("a", "action");
    go.href = portal.url;
    go.target = "_blank";
    go.rel = "noopener noreferrer";
    go.textContent = portal.kind === "bank_portal" ? "前往銀行登錄頁" : "前往登錄";
    if (portal.hint) go.title = portal.hint;
    foot.append(go);
    if (portal.kind === "bank_portal") {
      foot.append(el("span", "hint", portal.hint || "此為統一登錄頁，到站後請找這筆活動"));
    }
  }
  foot.append(registeredToggle(entry));

  const details = el("button", "action ghost", "活動條件");
  details.type = "button";
  details.setAttribute("aria-expanded", "false");
  const panel = el("div", "conditions");
  panel.hidden = true;
  details.addEventListener("click", () => {
    const open = panel.hidden;
    panel.hidden = !open;
    details.setAttribute("aria-expanded", String(open));
    if (open && !panel.dataset.loaded) void loadConditions(entry, panel);
  });
  foot.append(details);
  card.append(foot, panel);
  return card;
}

async function loadConditions(entry: AgendaEntry, panel: HTMLElement): Promise<void> {
  panel.dataset.loaded = "1";
  panel.textContent = "載入中…";
  try {
    const cached = offerDetails.get(entry.id);
    const offer = cached ?? (await fetchCatalog(entry.bank_id)).offers.find((o) => o.id === entry.id);
    panel.replaceChildren();
    if (!offer) {
      panel.append(el("p", "", "找不到這筆活動的條件資料。"));
      return;
    }
    panel.append(...conditionRows(offer));
  } catch {
    panel.replaceChildren(el("p", "", "條件資料載入失敗，請重新整理。"));
  }
}

function conditionRows(offer: CatalogOffer): HTMLElement[] {
  const rows: HTMLElement[] = [];
  const conditions = offer.conditions ?? {};
  const contract = offer.registration?.contract;

  const row = (label: string, value: string) => {
    const line = el("div", "cond");
    line.append(el("span", "cond-label", label), el("span", "cond-value", value));
    return line;
  };

  if (contract?.kind) {
    rows.push(row("時序", `${CONTRACT_LABEL[contract.kind]} — ${CONTRACT_NOTE[contract.kind]}`));
    if (contract.spend_counts_from) {
      rows.push(row("消費起算", formatMoment(contract.spend_counts_from)));
    }
    if (contract.last_chance_to_register) {
      rows.push(row("最晚登錄", formatMoment(contract.last_chance_to_register)));
    }
    if (contract.spend_days_left_after_registering !== undefined) {
      rows.push(row("登錄截止後", `還有 ${contract.spend_days_left_after_registering} 天可消費`));
    }
    if (contract.grace_days_after_period_end !== undefined) {
      rows.push(row("活動結束後", `還能補登錄 ${contract.grace_days_after_period_end} 天`));
    }
  }

  const eligibility = conditions.eligibility;
  if (eligibility) {
    const flags: string[] = [];
    if (eligibility.new_customer_only) flags.push("新戶限定");
    if (eligibility.first_swipe_only) flags.push("首刷限定");
    if (eligibility.primary_card_only) flags.push("限正卡人");
    if (flags.length) rows.push(row("資格", flags.join("、")));
    if (eligibility.cards?.length) rows.push(row("指定卡別", eligibility.cards.join("、")));
  }

  const tiers = conditions.threshold_tiers ?? [];
  if (tiers.length) {
    const table = el("div", "tiers");
    for (const tier of tiers) {
      const line = el("div", "tier");
      line.append(el("span", "tier-spend", `滿 ${formatMoney(tier.spend_twd)}`));
      const parts: string[] = [];
      if (tier.reward_twd !== undefined) parts.push(`回饋 ${formatMoney(tier.reward_twd)}`);
      if (tier.reward_if_installment !== undefined) {
        const periods = tier.installment_periods ? `分${tier.installment_periods}期以上` : "分期";
        parts.push(`${periods} ${formatMoney(tier.reward_if_installment)}`);
      }
      if (tier.quota_seats !== undefined) parts.push(`限 ${tier.quota_seats} 名`);
      line.append(el("span", "tier-reward", parts.join(" · ") || "—"));
      table.append(line);
    }
    const wrap = el("div", "cond");
    wrap.append(el("span", "cond-label", "階梯門檻"), table);
    rows.push(wrap);
  } else if (conditions.threshold_kind) {
    rows.push(row("門檻類型", conditions.threshold_kind === "cumulative" ? "累積滿額" : "單筆滿額"));
  }

  const installment = conditions.installment;
  if (installment?.required || installment?.periods?.length) {
    const parts: string[] = [];
    if (installment.required) parts.push("需分期");
    if (installment.periods?.length) parts.push(`可分 ${installment.periods.join("／")} 期`);
    if (installment.rate) parts.push(installment.rate);
    rows.push(row("分期", parts.join(" · ")));
  }

  if (conditions.quota?.limited) {
    rows.push(row("名額", conditions.quota.seats ? `限量 ${conditions.quota.seats} 名` : "限量，額滿為止"));
  }
  if (conditions.reward_cap_percent !== undefined) {
    rows.push(row("最高回饋率", `${conditions.reward_cap_percent}%`));
  }
  if (conditions.reward_cap_twd !== undefined) {
    rows.push(row("回饋上限", formatMoney(conditions.reward_cap_twd)));
  }

  if (!rows.length) rows.push(el("p", "", "官方頁未提供可結構化的條件資訊，請點標題查看原文。"));

  const source = el("p", "cond-source");
  const link = el("a");
  link.href = offer.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "以官方公告為準 ↗";
  source.append(link);
  rows.push(source);
  return rows;
}

/** CatalogOffer → 渲染用的 AgendaEntry。兩者共用大部分欄位。 */
function toEntry(bankId: string, offer: CatalogOffer): AgendaEntry {
  const registration = offer.registration;
  const quota = offer.conditions?.quota;
  const recurrence = registration?.recurrence?.kind;
  return {
    id: offer.id,
    bank_id: bankId,
    title: offer.title,
    url: offer.url,
    period: offer.period,
    windows: registration?.windows,
    recurrence:
      recurrence === "monthly" || recurrence === "per_campaign_period" ? recurrence : undefined,
    contract: registration?.contract?.kind,
    quota_limited: quota?.limited,
    quota_seats: quota?.seats,
    needs_review: offer.needs_review,
    review_codes: offer.review_codes,
  };
}

async function loadAllOffers(): Promise<AgendaEntry[]> {
  if (allOffers) return allOffers;
  const data = state.data;
  if (!data) return [];
  const payloads = await Promise.all(
    data.sources.map((source) =>
      fetchCatalog(source.bank_id).catch(() => ({ bank_id: source.bank_id, offers: [] })),
    ),
  );
  const entries: AgendaEntry[] = [];
  for (const payload of payloads) {
    for (const offer of payload.offers ?? []) {
      offerDetails.set(offer.id, offer);
      entries.push(toEntry(payload.bank_id, offer));
    }
  }
  allOffers = entries;
  return entries;
}

async function fetchCatalog(bankId: string): Promise<CatalogPayload> {
  let pending = catalogCache.get(bankId);
  if (!pending) {
    pending = fetch(`${base()}/data/catalog/${bankId}.json`, { cache: "no-store" }).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json() as Promise<CatalogPayload>;
    });
    catalogCache.set(bankId, pending);
  }
  return pending;
}

// ── 各分頁 ──────────────────────────────────────────────

function renderTimeline(root: HTMLElement): void {
  const data = state.data;
  if (!data) return;
  const today = taipeiDay();
  const days = [0, 1, 2, 3, 4, 5, 6].map((offset) => addDays(today, offset));
  const actionable = data.agenda.filter((entry) => !entry.needs_review && matches(entry, data.agenda));

  let shown = 0;
  for (const [index, day] of days.entries()) {
    const entries = actionable
      .map((entry) => ({ entry, windows: windowsOn(entry, day) }))
      .filter((item) => item.windows.length > 0)
      .sort((a, b) => {
        const left = anchorOf(a.windows[0]!)?.getTime() ?? 0;
        const right = anchorOf(b.windows[0]!)?.getTime() ?? 0;
        return left - right;
      });
    if (!entries.length) continue;
    shown += entries.length;

    const section = el("section", "day");
    const heading = el("h2", "day-head");
    const label = index === 0 ? "今日" : index === 1 ? "明日" : formatDay(day);
    heading.append(el("span", "day-label", label));
    heading.append(el("span", "day-date", formatDate(day)));
    heading.append(el("span", "day-count", `${entries.length} 個登錄時點`));
    section.append(heading);
    for (const item of entries) {
      const card = offerCard(item.entry, { compact: true });
      const first = item.windows[0]!;
      card.prepend(el("div", "clock", formatClock(first.start ?? first.end)));
      section.append(card);
    }
    root.append(section);
  }

  if (!shown) {
    const empty = el("div", "empty");
    empty.append(el("strong", "", "未來 7 天沒有已確認的登錄時點"));
    empty.append(
      el("p", "", "可切到「活動目錄」瀏覽全部活動，或看「需人工確認」——那裡是官方沒公告時間的活動。"),
    );
    root.append(empty);
  }

  const later = actionable.filter((entry) => {
    const next = nextWindow(entry);
    const anchor = next ? anchorOf(next) : null;
    return anchor !== null && taipeiDay(anchor) > days[6]!;
  });
  if (later.length) {
    const note = el("p", "later", `另有 ${later.length} 筆活動的登錄時點在 7 天後。`);
    root.append(note);
  }
}

function renderCatalog(root: HTMLElement, entries: AgendaEntry[]): void {
  const total = entries.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  state.page = Math.min(state.page, pages);
  const slice = entries.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);

  const count = el("p", "result-count");
  count.setAttribute("aria-live", "polite");
  count.textContent = `${total} 筆活動${pages > 1 ? `，第 ${state.page}／${pages} 頁` : ""}`;
  root.append(count);

  if (!slice.length) {
    root.append(el("div", "empty", "沒有符合條件的活動。"));
    return;
  }
  const list = el("div", "cards");
  for (const entry of slice) list.append(offerCard(entry));
  root.append(list);

  if (pages > 1) {
    const nav = el("nav", "pager");
    nav.setAttribute("aria-label", "分頁");
    const button = (label: string, target: number, disabled: boolean) => {
      const btn = el("button", "page", label);
      btn.type = "button";
      btn.disabled = disabled;
      btn.addEventListener("click", () => {
        state.page = target;
        writeUrl();
        render();
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
      return btn;
    };
    nav.append(button("上一頁", state.page - 1, state.page <= 1));
    nav.append(el("span", "page-info", `${state.page} / ${pages}`));
    nav.append(button("下一頁", state.page + 1, state.page >= pages));
    root.append(nav);
  }
}

function renderReview(root: HTMLElement, entries: AgendaEntry[]): void {
  root.append(
    el(
      "p",
      "explainer",
      "這些活動的登錄時間或期間無法從官方頁可靠判讀。刻意不放進時間軸與行事曆 —— " +
        "依據未確認的時間去搶登錄，比不提醒更糟。請點標題到官方頁確認。",
    ),
  );
  renderCatalog(root, entries);
}

function renderSources(root: HTMLElement): void {
  const data = state.data;
  if (!data) return;
  const table = el("div", "sources");
  for (const source of [...data.sources].sort((a, b) => (b.offer_count ?? 0) - (a.offer_count ?? 0))) {
    const row = el("div", `source status-${source.status}`);
    row.append(el("span", "dot"));
    const name = el("a", "source-name");
    name.href = source.entry_url ?? "#";
    name.target = "_blank";
    name.rel = "noopener noreferrer";
    name.textContent = source.bank_name;
    row.append(name);
    row.append(el("span", "source-count", `${source.offer_count ?? 0} 筆`));
    const label = { complete: "正常", partial: "部分可讀", failed: "讀取失敗", blocked: "被阻擋" }[
      source.status
    ];
    row.append(el("span", "source-status", label));
    if (source.message) row.append(el("p", "source-note", source.message));
    table.append(row);
  }
  root.append(table);

  const alerts = data.alerts ?? [];
  if (alerts.length) {
    const box = el("section", "alerts");
    box.append(el("h2", "", `來源警示（${alerts.length}）`));
    for (const alert of alerts.slice(0, 30)) {
      box.append(el("p", "", `${alert.bank_name}：${alert.message}`));
    }
    if (alerts.length > 30) box.append(el("p", "", `另有 ${alerts.length - 30} 則。`));
    root.append(box);
  }
}

// ── 外框 ────────────────────────────────────────────────

function renderControls(): void {
  const data = state.data;
  if (!data) return;
  const bar = document.querySelector<HTMLElement>("#controls");
  if (!bar) return;
  bar.replaceChildren();

  const search = el("input", "search");
  search.type = "search";
  search.placeholder = "搜尋活動或銀行";
  search.value = state.query;
  search.setAttribute("aria-label", "搜尋活動或銀行");
  let timer = 0;
  search.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      state.query = search.value.trim();
      state.page = 1;
      writeUrl(true);
      render();
    }, 150);
  });
  bar.append(search);

  const bankSelect = el("select", "select");
  bankSelect.setAttribute("aria-label", "篩選銀行");
  bankSelect.append(new Option("全部銀行", ""));
  for (const source of data.sources) {
    bankSelect.append(new Option(`${source.bank_name}（${source.offer_count ?? 0}）`, source.bank_id));
  }
  bankSelect.value = state.bank;
  bankSelect.addEventListener("change", () => {
    state.bank = bankSelect.value;
    state.page = 1;
    writeUrl();
    render();
  });
  bar.append(bankSelect);

  const needSelect = el("select", "select");
  needSelect.setAttribute("aria-label", "篩選條件");
  needSelect.append(new Option("不限條件", "all"));
  needSelect.append(new Option("有確定登錄時點", "registration"));
  needSelect.append(new Option("限量／要搶", "quota"));
  needSelect.append(new Option("先登錄後消費", "before-spend"));
  needSelect.value = state.need;
  needSelect.addEventListener("change", () => {
    state.need = needSelect.value as State["need"];
    state.page = 1;
    writeUrl();
    render();
  });
  bar.append(needSelect);

  const mine = el("label", "toggle");
  const box = el("input");
  box.type = "checkbox";
  box.checked = state.myBanksOnly;
  box.addEventListener("change", () => {
    state.myBanksOnly = box.checked;
    state.page = 1;
    writeUrl();
    render();
  });
  mine.append(box, el("span", "", "只看我的銀行"));
  bar.append(mine);

  const pick = el("button", "action ghost", "設定我的銀行");
  pick.type = "button";
  pick.addEventListener("click", () => togglePicker());
  bar.append(pick);
}

function togglePicker(): void {
  const panel = document.querySelector<HTMLElement>("#picker");
  const data = state.data;
  if (!panel || !data) return;
  if (!panel.hidden) {
    panel.hidden = true;
    return;
  }
  panel.replaceChildren();
  panel.append(
    el("p", "explainer", "勾選你實際持卡的銀行。只存在這台裝置的瀏覽器裡，不會上傳。"),
  );
  const mine = readSet(MY_BANKS_KEY);
  const grid = el("div", "picker-grid");
  for (const source of data.sources) {
    const label = el("label", "pick");
    const box = el("input");
    box.type = "checkbox";
    box.checked = mine.has(source.bank_id);
    box.addEventListener("change", () => {
      const set = readSet(MY_BANKS_KEY);
      if (box.checked) set.add(source.bank_id);
      else set.delete(source.bank_id);
      writeSet(MY_BANKS_KEY, set);
      render();
    });
    label.append(box, el("span", "", source.bank_name));
    grid.append(label);
  }
  panel.append(grid);
  panel.hidden = false;
}

function renderTabs(): void {
  const data = state.data;
  if (!data) return;
  const nav = document.querySelector<HTMLElement>("#tabs");
  if (!nav) return;
  nav.replaceChildren();
  const tabs: Array<[Tab, string, number | null]> = [
    ["timeline", "登錄時間軸", null],
    ["catalog", "活動目錄", data.counts.offers - data.counts.needs_review],
    ["review", "需人工確認", data.counts.needs_review],
    ["sources", "來源狀態", data.sources.length],
  ];
  for (const [id, label, count] of tabs) {
    const btn = el("button", "tab", count === null ? label : `${label}（${count}）`);
    btn.type = "button";
    btn.setAttribute("aria-pressed", String(state.tab === id));
    btn.addEventListener("click", () => {
      state.tab = id;
      state.page = 1;
      writeUrl();
      render();
    });
    nav.append(btn);
  }
}

function renderMeta(): void {
  const data = state.data;
  if (!data) return;
  const hours = stalenessHours(data.generated_at);
  const banner = document.querySelector<HTMLElement>("#staleness");
  if (banner) {
    if (hours > STALE_HOURS) {
      banner.hidden = false;
      banner.textContent =
        `資料更新於 ${formatMoment(data.generated_at)}，已超過 ${Math.floor(hours)} 小時。` +
        "活動狀態可能已變動，請以官方公告為準。";
    } else {
      banner.hidden = true;
    }
  }
  const updated = document.querySelector<HTMLElement>("#updated");
  if (updated) updated.textContent = `資料更新：${formatMoment(data.generated_at)}`;

  const stats = document.querySelector<HTMLElement>("#stats");
  if (stats) {
    stats.replaceChildren();
    const items: Array<[string, number]> = [
      ["需登錄活動", data.counts.registration_required],
      ["已確認登錄時點", data.counts.with_window],
      ["可直接行動", data.counts.actionable_with_window],
      ["收錄活動", data.counts.offers],
    ];
    for (const [label, value] of items) {
      const box = el("div", "stat");
      box.append(el("strong", "", String(value)), el("span", "", label));
      stats.append(box);
    }
  }
}

function render(): void {
  void renderAsync();
}

async function renderAsync(): Promise<void> {
  const data = state.data;
  const root = document.querySelector<HTMLElement>("#view");
  if (!data || !root) return;
  renderTabs();
  renderMeta();

  if (state.tab === "timeline") {
    root.replaceChildren();
    renderTimeline(root);
    return;
  }
  if (state.tab === "sources") {
    root.replaceChildren();
    renderSources(root);
    return;
  }

  // 目錄與需人工確認要看全部 815 筆，不只 index.json 裡有登錄時點的那些
  if (!allOffers) root.replaceChildren(el("div", "empty", "載入完整活動目錄中…"));
  const pool = (await loadAllOffers()).filter((entry) => matches(entry, []));
  root.replaceChildren();
  const entries =
    state.tab === "review"
      ? pool.filter((entry) => entry.needs_review)
      : pool.filter((entry) => !entry.needs_review);
  if (state.tab === "review") renderReview(root, entries);
  else renderCatalog(root, entries);
}

async function init(): Promise<void> {
  readUrl();
  renderControls();
  const root = document.querySelector<HTMLElement>("#view");
  try {
    const response = await fetch(`${base()}/data/index.json`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = (await response.json()) as IndexPayload;
  } catch {
    if (root) {
      root.replaceChildren(el("div", "empty", "目前無法載入活動資料，請稍後重新整理。"));
    }
    return;
  }
  renderControls();
  render();

  const feed = document.querySelector<HTMLAnchorElement>("#subscribe");
  if (feed) {
    const absolute = new URL(`${base()}/calendar/registration.ics`, location.href);
    feed.href = `webcal://${absolute.host}${absolute.pathname}`;
    feed.dataset.https = absolute.href;
  }
  const download = document.querySelector<HTMLAnchorElement>("#download-ics");
  if (download) download.href = `${base()}/calendar/registration.ics`;

  window.addEventListener("popstate", () => {
    readUrl();
    renderControls();
    render();
  });
  // 跨日時自動重算（分頁在背景放到隔天也要正確）
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) render();
  });
  window.setInterval(render, 300000);
}

void init();
