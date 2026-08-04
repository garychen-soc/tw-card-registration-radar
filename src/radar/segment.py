"""明細頁的兩層切分。

**第一層：活動邊界。** 台灣銀行的活動頁普遍是單頁多活動 —— 實測玉山 momo 頁
含「活動一／活動二／活動三」各有自己的登錄時間與名額，中信一頁 14 個活動。
前身以明細頁 URL 雜湊當活動 ID，一頁一筆，各子活動的登錄時點互相污染，
產生「活動至 08-15 卻有 08-17 登錄時點」這類矛盾資料。

**第二層：段落。** 把每個活動區塊切成 期間／資格／辦法／登錄／名額／注意事項。
前身的 ``_registration_excerpt`` 只保留含「登錄」二字的行，結果活動辦法整段
在進入資料前就消失（``registration_text`` 中位數僅 245 字元，2400 字上限
從未被觸及）。

**為什麼吃純文字而不是 CSS selector：** 實測 7 家明細頁，只有聯邦與中信有
可辨識的 ``h2``/``dt``/``th`` 段落標題，但 6 家的純文字都含段落關鍵字。
DOM 標題只當加分項，不能當唯一依據。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .parse.normalize import normalize

# 活動邊界的候選錨點，特異性由高而低。adapter spec 可用 detail.boundary 覆寫。
GENERIC_BOUNDARIES: tuple[str, ...] = (
    r"【活動[一二三四五六七八九十\d]+[^】]{0,20}】",
    r"【[^】]{2,24}】",
    r"(?:^|\s)活動[一二三四五六七八九十](?=[：:、，\s])",
    r"(?:^|\s)◆\s*活動",
    r"(?:^|\s)第[一二三四五六七八九十\d]+檔",
)

SECTION_ANCHORS: tuple[tuple[str, str], ...] = (
    ("period", r"(?:活動|優惠|消費)(?:期間|日期|時間)"),
    ("registration", r"登錄(?:辦法|時間|期間|方式|日期)|活動登錄|開放登錄"),
    ("eligibility", r"參加資格|活動對象|參加對象|適用對象|適用卡|適用卡別|限定卡"),
    ("terms", r"活動辦法|活動內容|優惠內容|活動方式|回饋辦法|優惠說明|活動說明"),
    ("quota", r"名額|限量"),
    ("notes", r"注意事項|備註|其他事項|重要說明"),
)

_ANCHOR_SCANNER = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern in SECTION_ANCHORS)
)


@dataclass(frozen=True)
class OfferChunk:
    """一個子活動的文字區塊。"""

    title: str
    text: str
    boundary_marker: str = ""

    @property
    def split(self) -> bool:
        """是否真的由邊界切出來（而非整頁單一活動）。"""
        return bool(self.boundary_marker)


def split_offers(
    raw_text: str, *, pattern: str | None = None, min_chunk_chars: int = 40
) -> list[OfferChunk]:
    """把明細頁文字切成多個子活動區塊。

    切不出來時回傳單一區塊，且 ``boundary_marker`` 為空 —— 呼叫端據此決定
    是否要標記 ``needs_review``（「本頁含多個活動，請至官方頁確認」）。
    """
    text = normalize(raw_text)
    candidates = (pattern,) if pattern else GENERIC_BOUNDARIES

    for candidate in candidates:
        if candidate is None:
            continue
        matches = list(re.finditer(candidate, text, flags=re.MULTILINE))
        if len(matches) < 2:
            continue
        chunks: list[OfferChunk] = []
        bounds = [m.start() for m in matches] + [len(text)]
        for index, match in enumerate(matches):
            body = text[bounds[index] : bounds[index + 1]].strip()
            if len(body) < min_chunk_chars:
                continue
            chunks.append(
                OfferChunk(
                    title=_chunk_title(body),
                    text=body,
                    boundary_marker=match.group(0).strip(),
                )
            )
        if len(chunks) >= 2:
            return chunks

    stripped = text.strip()
    return [OfferChunk(title=_chunk_title(stripped), text=stripped)]


def _chunk_title(body: str) -> str:
    """取第一個有實質內容的行當標題。

    實測聯邦的頁面上有子活動的首行只是不可見字元，直接取第一行會產出空標題。
    """
    for line in body.splitlines():
        candidate = line.strip()
        if len(candidate) >= 2:
            return candidate[:120]
    return body.strip()[:120] or "（無標題）"


def sections(raw_text: str) -> dict[str, str]:
    """依關鍵字錨點把文字切成段落。

    同名段落會合併（一頁可能有多個「注意事項」）。未命中任何錨點的前導文字
    放進 ``lead``，因為活動標題與摘要常在那裡。
    """
    text = normalize(raw_text)
    matches = list(_ANCHOR_SCANNER.finditer(text))
    if not matches:
        return {"lead": text.strip()} if text.strip() else {}

    result: dict[str, list[str]] = {}
    lead = text[: matches[0].start()].strip()
    if lead:
        result["lead"] = [lead]

    for index, match in enumerate(matches):
        name = match.lastgroup or "notes"
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start() : end].strip()
        if body:
            result.setdefault(name, []).append(body)

    return {name: "\n".join(parts) for name, parts in result.items()}


def registration_text(raw_text: str) -> str:
    """登錄相關段落，找不到就退回全文。

    刻意不做前身那種「只留含『登錄』的行」的過濾 —— 那會把判斷資格與門檻
    所需的段落一起丟掉。這裡只是**優先**回傳登錄段落，資訊不會消失。
    """
    parts = sections(raw_text)
    return parts.get("registration") or normalize(raw_text)


def table_rows(html: str) -> tuple[list[str], list[list[str]]]:
    """從 dt/dd 或 th/td 表格抽出表頭與資料列。

    實測聯邦銀行明細頁的欄位名直接就是欄位定義：
    ``單筆分期門檻 / 回饋刷卡金 / 分12期以上回饋升級 / 每波限量登錄名額``
    後面跟著四階資料。這是唯一能正確表達階梯門檻的來源。
    """
    cells = [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<(?:dt|dd|th|td)[^>]*>(.*?)</(?:dt|dd|th|td)>", html, re.S)
    ]
    cells = [normalize(cell) for cell in cells if cell.strip()]
    if not cells:
        return [], []

    header_like = re.compile(r"門檻|回饋|名額|限量|升級|期|登錄|時間|金額|條件")
    value_like = re.compile(r"^[\d,]+\s*(?:元|名|點|%|期)?$")

    headers: list[str] = []
    for cell in cells:
        if value_like.match(cell):
            break
        if header_like.search(cell):
            headers.append(cell)
        elif headers:
            break
    if not headers:
        return [], []

    # 只留看得出是資料值的儲存格。實測聯邦的表格在資料列之間夾著
    # 「第一波 8/15下午3點開放登錄至8/20」這種合併儲存格的說明文字，
    # 直接等寬切分會整體錯位。登錄時間不從表格取 —— 它由全文的時間解析負責。
    values = [cell for cell in cells[len(headers) :] if value_like.match(cell)]
    if not values:
        return headers, []

    # 表頭數可能多於每列的值數（最後一欄是跨列合併）。從表頭數往下試，
    # 取第一個能讓首欄金額嚴格遞增且不重複的寬度。
    for width in range(len(headers), 1, -1):
        rows = [
            values[index : index + width]
            for index in range(0, len(values) - width + 1, width)
        ]
        rows = [row for row in rows if len(row) == width]
        if len(rows) < 2:
            continue
        firsts = [_leading_int(row[0]) for row in rows]
        if any(value is None for value in firsts):
            continue
        ordered = [value for value in firsts if value is not None]
        if ordered == sorted(ordered) and len(set(ordered)) == len(ordered):
            return headers[:width], rows
    return headers, []


def _leading_int(cell: str) -> int | None:
    match = re.search(r"([\d,]+)", cell)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None
