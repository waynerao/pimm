# Inventory feed — one instance per market, thin FeedAdapter subclass

from pimm.feeds.base import FeedAdapter


class InventoryFeed(FeedAdapter):
    def __init__(self, engine_push, ric_list=None, market_name="ALL", thread=None, **kwargs):
        super().__init__("inventory", engine_push, ric_list=ric_list, market_name=market_name, thread=thread, **kwargs)
