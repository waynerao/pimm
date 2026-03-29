import enum
from dataclasses import dataclass


class Side(enum.Enum):
    BUY = "buy"
    SELL = "sell"


class PriceType(enum.Enum):
    BEST_BID = "best_bid"
    BEST_ASK = "best_ask"


@dataclass
class TradeFill:
    ric = ""
    side = Side.BUY
    fill_qty = 0.0
    fill_price = 0.0
    timestamp = None

    def __init__(self, ric, side, fill_qty, fill_price, timestamp):
        self.ric, self.side = ric, side
        self.fill_qty, self.fill_price = fill_qty, fill_price
        self.timestamp = timestamp


class EngineSnapshot:
    def __init__(self, markets, scaling, recent_fills, session_status, session_countdowns, feed_status,
                 timestamp, last_full_batch_times=None, delta_beta_info="", console_log=None,
                 market_configs=None, day_types=None, pimm_config=None):
        self.markets = markets
        self.scaling = scaling
        self.recent_fills = recent_fills
        self.session_status = session_status
        self.session_countdowns = session_countdowns
        self.feed_status = feed_status
        self.timestamp = timestamp
        self.last_full_batch_times = last_full_batch_times or {}
        self.delta_beta_info = delta_beta_info
        self.console_log = console_log or []
        self.market_configs = market_configs or {}
        self.day_types = day_types or {}
        self.pimm_config = pimm_config
