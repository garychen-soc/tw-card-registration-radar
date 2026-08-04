"""宣告式 adapter。

``listing`` 負責「怎麼拿到清單」，``runner`` 負責把清單變成 Campaign/Offer。
每家銀行的差異全部落在 ``sources/*.toml``，真正詭異的才需要 override。
"""

from .listing import Fetch, ListingItem, read_listing

__all__ = ["Fetch", "ListingItem", "read_listing"]
