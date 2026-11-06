"""共用頁的時點歸屬（審查 🟡-5：華南大玩家的時點掛在「華南卡xklook…等 3 個通路」名下）。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from radar.models import Evidence, Slot, Stub, SubActivity
from radar.pipeline import Doc, _group_title, _owner_title

TPE = ZoneInfo("Asia/Taipei")


def stub(title: str, key: str) -> Stub:
    return Stub(bank_id="hncb", key=key, title=title, url=f"https://x/{key}")


def sub(quote: str, name: str = "") -> SubActivity:
    slot = Slot(start=datetime(2026, 12, 20, 10, 0, tzinfo=TPE), end=None, kind="opens_at",
                origin="explicit", evidence=Evidence(quote=quote, url="https://x"))
    return SubActivity(name=name, slots=[slot])


def doc(text: str, *titles: str) -> Doc:
    stubs = [stub(t, f"k{i}") for i, t in enumerate(titles)]
    stubs.sort(key=lambda s: (-len(s.title), s.key))  # 與 group_docs 相同：最長者為主
    return Doc("hncb", stubs, text, "digest")


HUB = (
    "華南大玩家 天涼秋爽冬好遊\n【出國輕鬆遊】12/20 10:00 開放登錄，限量2,000名。\n"
    + "說明文字。" * 60
    + "\nKlook 全站商品單筆5,000送400，需填優惠碼。\n可樂旅遊高鐵假期享9折優惠，免登錄。"
)


def test_hub_page_slot_goes_to_the_section_it_appears_in() -> None:
    d = doc(HUB, "華南卡xklook，全站商品單筆5,000送400(需填優惠碼，限量)",
            "華南大玩家 天涼秋爽冬好遊", "可樂旅遊高鐵假期享9折優惠")
    owner = _owner_title(d, [sub("12/20 10:00 開放登錄", "【出國輕鬆遊】")])
    assert owner == "華南大玩家 天涼秋爽冬好遊"
    assert _group_title(d, owner) == "華南大玩家 天涼秋爽冬好遊 等 3 個通路"


def test_primary_title_already_heading_the_quote_is_kept() -> None:
    text = "Uber Eats／foodpanda 外送優惠\nfoodpanda 加碼：每月1日10:00開放登錄"
    d = doc(text, "Uber Eats", "foodpanda")
    assert _owner_title(d, [sub("每月1日10:00開放登錄")]) is None


def test_merchant_list_entry_is_not_taken_as_owner() -> None:
    text = (
        "立即登錄 家具/家飾 IKEA 宜得利家居 有情門 歐德傢俱 大成鋼隆美家居 億進寢具 "
        "活動一 分期新戶專屬禮 2026/10/15 15:00 起至2026/10/25 23:59 登錄"
    )
    d = doc(text, "樂享星生活 家具家電享分期 0% 利率 最高5%回饋！", "IKEA", "宜得利家居",
            "有情門", "歐德傢俱", "大成鋼隆美家居", "億進寢具")
    assert _owner_title(d, [sub("2026/10/15 15:00 起至2026/10/25 23:59")]) is None


def test_subs_pointing_to_different_cards_keep_original_title() -> None:
    filler = "。" * 300
    text = f"第一個活動標題\n11/1 10:00 開放登錄\n{filler}\n第二個活動的標題\n12/1 10:00 開放登錄"
    d = doc(text, "第一個活動標題", "第二個活動的標題")
    subs = [sub("11/1 10:00 開放登錄"), sub("12/1 10:00 開放登錄")]
    assert _owner_title(d, subs) is None
