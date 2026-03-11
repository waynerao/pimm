import enum
from dataclasses import dataclass


class Side(enum.Enum):
    BUY = "buy"
    SELL = "sell"


class PriceType(enum.Enum):
    BEST_BID = "best_bid"
    MID = "mid"
    BEST_OFFER = "best_offer"


@dataclass
class TradeFill:
    ric = ""
    side = Side.BUY
    fill_qty = 0.0
    fill_price = 0.0
    timestamp = None

    def __init__(self, ric, side, fill_qty, fill_price, timestamp):
        self.ric = ric
        self.side = side
        self.fill_qty = fill_qty
        self.fill_price = fill_price
        self.timestamp = timestamp


class EngineSnapshot:
    # Snapshot of engine state pushed to GUI via mp.Queue
    # Contains per-market DataFrames and global summary

    def __init__(
        self,
        markets,
        scaling,
        recent_fills,
        session_status,
        session_countdowns,
        feed_status,
        timestamp,
        last_full_batch_times=None,
    ):
        self.markets = markets
        self.scaling = scaling
        self.recent_fills = recent_fills
        self.session_status = session_status
        self.session_countdowns = session_countdowns
        self.feed_status = feed_status
        self.timestamp = timestamp
        self.last_full_batch_times = last_full_batch_times or {}
