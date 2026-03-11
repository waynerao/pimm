# Alpha signal feed adapter (queue-only, no desktool thread)
# External alpha project pushes DataFrames into data_queue directly.

import pandas as pd

from pimm.feeds.base import FeedAdapter


class AlphaFeed(FeedAdapter):

    def __init__(self, engine_push, rics=None):
        super().__init__(event_type="alpha", engine_push=engine_push)
        self._rics = rics or []

    def start(self, loop):
        super().start(loop)
        # Push zero-alpha stub on startup for known rics
        if self._rics:
            stub = pd.DataFrame({"ric": self._rics, "alpha": [0.0] * len(self._rics)})
            self._push(stub)
