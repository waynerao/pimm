# Live price feed adapter (desktool thread, KDB+ tick)

from pimm.feeds.base import FeedAdapter


class LivePriceFeed(FeedAdapter):
    def __init__(self, engine_push, thread=None, **kwargs):
        super().__init__(event_type="live_price", engine_push=engine_push, thread=thread, **kwargs)
