# Trade fill feed adapter (desktool stub)

from pimm.feeds.base import FeedAdapter


class FillsFeed(FeedAdapter):

    def __init__(self, engine_push):
        super().__init__(event_type="fills", engine_push=engine_push)

    def _subscribe(self):
        # TODO: Wire to desktool.subscribe_fills(callback=self._push)
        pass

    def on_update(self, df):
        self._push(df)
