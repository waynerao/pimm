# Risk appetite feed adapter (desktool stub)

from pimm.feeds.base import FeedAdapter


class RiskAppetiteFeed(FeedAdapter):

    def __init__(self, engine_push):
        super().__init__(event_type="risk_appetite", engine_push=engine_push)

    def _subscribe(self):
        # TODO: desktool.subscribe(self._data_queue, feed_type="risk_appetite", ...)
        pass
