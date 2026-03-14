import logging

import pandas as pd

logger = logging.getLogger(__name__)


class StateManager:
    # Manages the universe DataFrame — single source of truth

    COLUMNS = [
        "quote_status", "remark",
        "lot_size", "stock_limit",
        "buy_state", "sell_state",
        "buy_raw", "sell_raw",
        "last_price", "fx_rate",
        "alpha", "inventory",
        "live_buy_qty", "live_sell_qty",
        "last_sent_time",
        "filled_buy_since_dispatch",
        "filled_sell_since_dispatch",
        "pnl_buy_qty", "pnl_buy_cost",
        "pnl_sell_qty", "pnl_sell_revenue",
    ]

    def __init__(self, ric_list, lot_sizes, config):
        self.df = pd.DataFrame(
            index=pd.Index(ric_list, name="ric")
        )
        self.df["quote_status"] = True
        self.df["remark"] = ""
        for ric in ric_list:
            if ric in lot_sizes:
                self.df.at[ric, "lot_size"] = lot_sizes[ric]
            else:
                self.df.at[ric, "lot_size"] = float("nan")
                self.df.at[ric, "quote_status"] = False
                self.df.at[ric, "remark"] = "no lot size"
                logger.warning(
                    f"RIC {ric} has no lot size, "
                    f"quote_status=False"
                )
        self.df["stock_limit"] = [
            float(config.get_stock_limit(r)) for r in ric_list
        ]
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
        for _, row in feed_df.iterrows():
            ric = str(row["ric"])
            if ric not in self.df.index:
                continue
            self.df.at[ric, "buy_state"] = str(row["buy_state"])
            self.df.at[ric, "sell_state"] = str(row["sell_state"])
            self.df.at[ric, "buy_raw"] = float(row["buy_qty"])
            self.df.at[ric, "sell_raw"] = float(row["sell_qty"])
            if "fx_rate" in row.index:
                self.df.at[ric, "fx_rate"] = float(row["fx_rate"])

    def update_live_price(self, feed_df):
        for _, row in feed_df.iterrows():
            ric = str(row["ric"])
            if ric not in self.df.index:
                continue
            self.df.at[ric, "last_price"] = float(row["last_price"])

    def update_inventory(self, feed_df):
        for _, row in feed_df.iterrows():
            ric = str(row["ric"])
            if ric not in self.df.index:
                continue
            self.df.at[ric, "inventory"] = float(row["inventory"])

    def update_alpha(self, feed_df):
        for _, row in feed_df.iterrows():
            ric = str(row["ric"])
            if ric not in self.df.index:
                continue
            alpha = max(-1.0, min(1.0, float(row["alpha"])))
            self.df.at[ric, "alpha"] = alpha

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
