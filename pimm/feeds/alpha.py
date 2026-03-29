# Alpha signal feed — one instance per market, thin FeedAdapter subclass

import pandas as pd

from pimm.feeds.base import FeedAdapter


class AlphaFeed(FeedAdapter):
    def __init__(self, engine_push, ric_list=None, market_name="ALL", thread=None, **kwargs):
        super().__init__("alpha", engine_push, ric_list=ric_list, market_name=market_name, thread=thread, **kwargs)

    def start(self, loop):
        super().start(loop)
        if self._ric_list:
            stub = pd.DataFrame({"ric": self._ric_list, "alpha": [0.0] * len(self._ric_list)})
            self._push(stub)
