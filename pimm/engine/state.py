import logging

import pandas as pd

logger = logging.getLogger(__name__)


def _get_stub_lot_sizes(ric_list):
    """Stub: replace with desktool.get_lot_size(ric_list) when wired."""
    stub = {"0005.HK": 400, "0700.HK": 100, "9988.HK": 100, "1299.HK": 500, "0388.HK": 100,
            "600519.SS": 100, "601318.SS": 100, "000858.SZ": 100, "600036.SS": 100, "000333.SZ": 100,
            "2330.TW": 1000, "2317.TW": 1000, "2454.TW": 1000, "2412.TW": 1000, "3711.TW": 1000}
    return {r: stub[r] for r in ric_list if r in stub}


class StateManager:
    # Manages the universe DataFrame — single source of truth

    COLUMNS = [
        "quote_status",
        "remark",
        "lot_size",
        "stock_limit",
        "buy_state",
        "sell_state",
        "buy_raw",
        "sell_raw",
        "last_price",
        "fx_rate",
        "alpha",
        "inventory",
        "live_buy_qty",
        "live_sell_qty",
        "last_sent_time",
        "filled_buy_since_dispatch",
        "filled_sell_since_dispatch",
        "pnl_buy_qty",
        "pnl_buy_cost",
        "pnl_sell_qty",
        "pnl_sell_revenue",
    ]

    def __init__(self, ric_list, config):
        self.df = pd.DataFrame(index=pd.Index(ric_list, name="ric"))
        lot_sizes = _get_stub_lot_sizes(ric_list)
        self.df["lot_size"] = self.df.index.map(lot_sizes)
        self.df["quote_status"] = self.df["lot_size"].notna()
        self.df["remark"] = ""
        self.df.loc[~self.df["quote_status"], "remark"] = "no lot size"
        missing = list(self.df[~self.df["quote_status"]].index)
        if missing:
            logger.warning(f"[{config.name}] No lot size: {missing}")
        self.df["stock_limit"] = self.df.index.map(lambda r: float(config.get_stock_limit(r)))
        n_lots = int(self.df["quote_status"].sum())
        logger.info(f"[{config.name}] Universe: {len(ric_list)} RICs, {n_lots} with lot sizes")
        self.df["buy_state"] = ""
        self.df["sell_state"] = ""
        self.df["buy_raw"] = 0.0
        self.df["sell_raw"] = 0.0
        self.df["last_price"] = 0.0
        self.df["fx_rate"] = 1.0
        self.df["alpha"] = 0.0
        self.df["inventory"] = 0.0
        self.df["live_buy_qty"] = 0.0
        self.df["live_sell_qty"] = 0.0
        self.df["last_sent_time"] = pd.NaT
        self.df["filled_buy_since_dispatch"] = 0.0
        self.df["filled_sell_since_dispatch"] = 0.0
        self.df["pnl_buy_qty"] = 0.0
        self.df["pnl_buy_cost"] = 0.0
        self.df["pnl_sell_qty"] = 0.0
        self.df["pnl_sell_revenue"] = 0.0

    @property
    def quotable(self):
        return self.df[self.df["quote_status"] == True]  # noqa: E712

    def update_risk_appetite(self, feed_df):
        if "ric" not in feed_df.columns:
            return False
        incoming = feed_df.set_index("ric")
        common = self.df.index.intersection(incoming.index)
        if common.empty:
            return False
        src = incoming.loc[common]
        self.df.loc[common, "buy_state"] = src["buy_state"].astype(str)
        self.df.loc[common, "sell_state"] = src["sell_state"].astype(str)
        self.df.loc[common, "buy_raw"] = src["buy_qty"].astype(float)
        self.df.loc[common, "sell_raw"] = src["sell_qty"].astype(float)
        if "fx_rate" in src.columns:
            self.df.loc[common, "fx_rate"] = src["fx_rate"].astype(float)
        return True

    def update_live_price(self, feed_df):
        if "ric" not in feed_df.columns:
            return False
        incoming = feed_df.set_index("ric")
        common = self.df.index.intersection(incoming.index)
        if common.empty:
            return False
        self.df.loc[common, "last_price"] = incoming.loc[common, "last_price"].astype(float)
        return True

    def update_inventory(self, feed_df):
        if "ric" not in feed_df.columns:
            return False
        incoming = feed_df.set_index("ric")
        common = self.df.index.intersection(incoming.index)
        if common.empty:
            return False
        self.df.loc[common, "inventory"] = incoming.loc[common, "inventory"].astype(float)
        return True

    def update_alpha(self, feed_df):
        if "ric" not in feed_df.columns:
            return False
        incoming = feed_df.set_index("ric")
        common = self.df.index.intersection(incoming.index)
        if common.empty:
            return False
        vals = incoming.loc[common, "alpha"].astype(float)
        self.df.loc[common, "alpha"] = vals.clip(-1.0, 1.0)
        return True

    def get_active_rics(self):
        mask = (self.df["buy_raw"] > 0) & (self.df["quote_status"] == True)  # noqa: E712
        return list(self.df.index[mask])

    def reset_pnl(self):
        self.df["pnl_buy_qty"] = 0.0
        self.df["pnl_buy_cost"] = 0.0
        self.df["pnl_sell_qty"] = 0.0
        self.df["pnl_sell_revenue"] = 0.0

    def copy(self):
        return self.df.copy()
