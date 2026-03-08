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
    # Contains a copy of the universe DataFrame instead of StockState dict

    def __init__(
        self,
        universe,
        buy_scaling,
        sell_scaling,
        recent_fills,
        session_active,
        session_end_countdown,
        feed_status,
        timestamp,
        last_full_batch_time=None,
    ):
        self.universe = universe            # pd.DataFrame copy
        self.buy_scaling = buy_scaling
        self.sell_scaling = sell_scaling
        self.recent_fills = recent_fills    # list of TradeFill
        self.session_active = session_active
        self.session_end_countdown = session_end_countdown
        self.feed_status = feed_status      # dict of feed_name -> status str
        self.timestamp = timestamp
        self.last_full_batch_time = last_full_batch_time
