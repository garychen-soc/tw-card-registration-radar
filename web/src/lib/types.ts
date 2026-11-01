/** v2 資料契約（對應 src/radar/models.py 與 src/radar/emit.py）。 */

export type SlotKind = "opens_at" | "range" | "deadline";
export type Reregister = "each_period" | "once" | "unknown";
export type Order = "register_before_spend" | "retroactive_ok" | "unknown";
export type SourceStatus = "ok" | "partial" | "failed" | "blocked";

export interface TimelineRow {
  aid: string;
  bank_id: string;
  bank: string;
  title: string;
  sub: string;
  sub_index: number;
  start: string | null;
  end: string | null;
  kind: SlotKind;
  precision: "minute" | "day";
  origin: "explicit" | "rule" | "enumerated";
  shifted: boolean;
  note: string;
  quote: string;
  url: string;
  portal: string;
  portal_hint: string;
  quota: number | null;
  reregister: Reregister;
  order: Order;
  stale: boolean;
}

export interface SourceHealth {
  bank_id: string;
  bank_name: string;
  status: SourceStatus;
  entry_url: string;
  stubs: number;
  activities: number;
  detail_failures: number;
  message: string;
  carried: number;
}

export interface Counts {
  activities: number;
  registration_required: number;
  with_slots: number;
  without_slots: number;
  review: number;
  stale: number;
}

export interface IndexData {
  schema_version: number;
  generated_at: string;
  timezone: string;
  counts: Counts;
  sources: SourceHealth[];
  timeline: TimelineRow[];
}

export interface Slot {
  start: string | null;
  end: string | null;
  kind: SlotKind;
  origin: string;
  precision: "minute" | "day";
  holiday_shifted: boolean;
  note: string;
  evidence: { quote: string; url: string };
}

export interface SubActivity {
  name: string;
  slots: Slot[];
  rules: string[];
  reregister: Reregister;
  reregister_quote: string;
  order: Order;
  order_quote: string;
  quota: number | null;
  quota_quote: string;
  review: string[];
}

export interface Activity {
  id: string;
  bank_id: string;
  bank_name: string;
  title: string;
  aliases: string[];
  url: string;
  period_start: string | null;
  period_end: string | null;
  summary: string;
  category: string;
  registration: "required" | "not_required" | "unknown";
  registration_quote: string;
  subs: SubActivity[];
  portal_url: string;
  portal_hint: string;
  review: string[];
  observed_at: string;
  stale: boolean;
}

export interface BankData {
  bank_id: string;
  activities: Activity[];
}
