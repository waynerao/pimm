# Alpha signal feed adapter (alphaflow stub)

import pandas as pd

from pimm.feeds.base import FeedAdapter


class AlphaFeed(FeedAdapter):

    def __init__(self, engine_push, rics=None):
        super().__init__(event_type="alpha", engine_push=engine_push)
        self._rics = rics or []

    def _subscribe(self):
        # TODO: Wire to alphaflow.subscribe(callback=self._push)
        # Push zero-alpha stub on startup
        if self._rics:
            stub = pd.DataFrame({"ric": self._rics, "alpha": [0.0] * len(self._rics)})
            self._push(stub)

    def on_update(self, df):
        self._push(df)
