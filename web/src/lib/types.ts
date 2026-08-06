/**
 * 對應 src/radar/emit.py 產出的 index.json。
 *
 * 兩件事必須跟後端契約一致：
 * 1. 預設值不會出現在 JSON 裡（emit 的 prune 會移除），所以幾乎每個欄位都是選用的。
 * 2. 資料裡沒有任何時間衍生狀態。「進行中」「今日待登錄」全在這裡算。
 */

export type WindowKind = "range" | "opens_at" | "deadline" | "recurring";

export type ContractKind =
  | "register_before_spend"
  | "retroactive_ok"
  | "registration_closes_early"
  | "per_period_reregister"
  | "unknown";

export interface Period {
  start?: string;
  end?: string;
  confidence?: number;
}

export interface RegWindow {
  kind: WindowKind;
  start?: string;
  end?: string;
  confidence?: number;
}

export interface AgendaEntry {
  id: string;
  bank_id: string;
  title: string;
  url: string;
  /** 同一份活動還公告在哪些網址。銀行會把同個活動區塊掛在多個子頁上
   *  （實測星展一檔活動掛 6 頁），後端合併成一筆並把其餘網址留在這裡。 */
  also_at?: string[];
  period?: Period;
  windows?: RegWindow[];
  recurrence?: "monthly" | "per_campaign_period";
  contract?: ContractKind;
  quota_limited?: boolean;
  quota_seats?: number;
  needs_review?: boolean;
  review_codes?: string[];
}

export interface Portal {
  url?: string;
  kind?: "activity_specific" | "bank_portal" | "unknown";
  hint?: string;
}

export interface SourceInfo {
  bank_id: string;
  bank_name: string;
  status: "complete" | "partial" | "failed" | "blocked";
  entry_url?: string;
  campaign_count?: number;
  /** 去重前抓到的筆數。這是涵蓋率健康度的數字，後端的發布防護拿它比對上一版，
   *  語意不可改 —— 要顯示給使用者的筆數請用 unique_offer_count。 */
  offer_count?: number;
  /** 去重後實際發布的筆數，與 catalog/<bank>.json 的筆數一致。 */
  unique_offer_count?: number;
  message?: string;
  portal?: Portal;
}

export interface Alert {
  type: string;
  bank_id: string;
  bank_name: string;
  message: string;
  url?: string;
}

export interface IndexPayload {
  schema_version: number;
  generated_at: string;
  timezone: string;
  counts: {
    campaigns: number;
    /** 去重前的筆數（涵蓋率基準）。顯示給使用者請用 unique_offers。 */
    offers: number;
    /** 去重後實際發布的筆數。其餘計數（with_window、needs_review…）都以此為母數。 */
    unique_offers?: number;
    duplicate_offers?: number;
    with_window: number;
    actionable_with_window: number;
    needs_review: number;
    registration_required: number;
  };
  sources: SourceInfo[];
  alerts?: Alert[];
  agenda: AgendaEntry[];
}

/** 目錄檔（catalog/<bank>.json）—— 使用者展開條件時才載入。 */
export interface ThresholdTier {
  spend_twd: number;
  reward_twd?: number;
  reward_percent?: number;
  reward_if_installment?: number;
  installment_periods?: number;
  quota_seats?: number;
}

export interface CatalogOffer {
  id: string;
  campaign_id: string;
  title: string;
  url: string;
  also_at?: string[];
  period?: Period;
  registration?: {
    required?: boolean;
    windows?: RegWindow[];
    recurrence?: { kind?: string; note?: string };
    contract?: {
      kind?: ContractKind;
      spend_counts_from?: string;
      last_chance_to_register?: string;
      spend_days_left_after_registering?: number;
      grace_days_after_period_end?: number;
      confidence?: number;
      consistency?: string[];
    };
  };
  conditions?: {
    eligibility?: {
      new_customer_only?: boolean;
      first_swipe_only?: boolean;
      primary_card_only?: boolean;
      cards?: string[];
    };
    threshold_kind?: string;
    threshold_tiers?: ThresholdTier[];
    installment?: { required?: boolean; periods?: number[]; rate?: string };
    quota?: { limited?: boolean; seats?: number };
    reward_cap_twd?: number;
    reward_cap_percent?: number;
  };
  needs_review?: boolean;
  review_codes?: string[];
}

export interface CatalogPayload {
  schema_version: number;
  bank_id: string;
  offers: CatalogOffer[];
}
