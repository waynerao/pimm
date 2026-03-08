# Live price feed adapter (KDB+ tick subscription stub)

from pimm.feeds.base import FeedAdapter


class LivePriceFeed(FeedAdapter):

    def __init__(self, engine_push):
        super().__init__(event_type="live_price", engine_push=engine_push)

    def _subscribe(self):
        # TODO: Wire to KDB+ real-time tick subscription
        pass

    def on_update(self, df):
        self._push(df)
