# Trade fills feed adapter (desktool thread)

from pimm.feeds.base import FeedAdapter


class FillsFeed(FeedAdapter):
    def __init__(self, engine_push, thread=None, **kwargs):
        super().__init__(event_type="fills", engine_push=engine_push, thread=thread, **kwargs)
