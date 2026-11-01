"""銀行擷取器註冊表。新增一家銀行＝新增一個模組並列入 ``MODULES``。"""

from __future__ import annotations

import importlib

from .base import Adapter

MODULES = (
    "cathay",
    "chb",
    "ctbc",
    "dbs",
    "esun",
    "first",
    "hncb",
    "kgi",
    "mega",
    "obank",
    "scsb",
    "sinopac",
    "sunny",
    "taipei_fubon",
    "taishin",
    "tcbbank",
    "ubot",
    "yuanta",
)


def load(bank_id: str) -> type[Adapter]:
    module = importlib.import_module(f"{__name__}.{bank_id}")
    adapter: type[Adapter] = module.ADAPTER
    if adapter.id != bank_id:
        raise ValueError(f"{bank_id} 模組的 ADAPTER.id 是 {adapter.id}")
    return adapter


def available() -> list[str]:
    found = []
    for bank_id in MODULES:
        try:
            importlib.import_module(f"{__name__}.{bank_id}")
        except ModuleNotFoundError:
            continue
        found.append(bank_id)
    return found
