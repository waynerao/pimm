import logging

import pandas as pd

logger = logging.getLogger(__name__)


class StateManager:
    # Manages the universe DataFrame — single source of truth for all per-stock state

    COLUMNS = [
        "lot_size", "stock_limit",
        "buy_state", "sell_state",
        "buy_raw", "sell_raw",
        "last_price", "fx_rate",
        "alpha", "inventory",
        "live_buy_qty", "live_sell_qty",
        "last_sent_time",
        "filled_buy_since_dispatch", "filled_sell_since_dispatch",
        "pnl_buy_qty", "pnl_buy_cost", "pnl_sell_qty", "pnl_sell_revenue",
    ]

    def __init__(self, ric_list, lot_sizes, config):
        # Build universe DataFrame from RIC list, lot sizes, and config
        # Only RICs with valid lot sizes are included
        valid_rics = []
        for ric in ric_list:
            if ric in lot_sizes:
                valid_rics.append(ric)
            else:
                logger.warning("RIC %s has no lot size, skipping", ric)

        self.df = pd.DataFrame(index=pd.Index(valid_rics, name="ric"))

        # Static columns from startup
        self.df["lot_size"] = [lot_sizes[r] for r in valid_rics]
        self.df["stock_limit"] = [
            float(config.get_stock_limit(r)) for r in valid_rics
        ]

        # Dynamic columns initialized to defaults
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

    def update_risk_appetite(self, feed_df):
        # Merge risk appetite data into universe DataFrame
        # feed_df columns: ric, buy_state, buy_qty, sell_state, sell_qty, fx_rate
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
        # Merge live price data: ric, last_price
        for _, row in feed_df.iterrows():
            ric = str(row["ric"])
            if ric not in self.df.index:
                continue
            self.df.at[ric, "last_price"] = float(row["last_price"])

    def update_inventory(self, feed_df):
        # Merge inventory data: ric, inventory
        for _, row in feed_df.iterrows():
            ric = str(row["ric"])
            if ric not in self.df.index:
                continue
            self.df.at[ric, "inventory"] = float(row["inventory"])

    def update_alpha(self, feed_df):
        # Merge alpha data: ric, alpha (clamped to [-1, 1])
        for _, row in feed_df.iterrows():
            ric = str(row["ric"])
            if ric not in self.df.index:
                continue
            alpha = max(-1.0, min(1.0, float(row["alpha"])))
            self.df.at[ric, "alpha"] = alpha

    def get_active_rics(self):
        # Return RICs that have received risk appetite data
        mask = self.df["buy_raw"] > 0
        return list(self.df.index[mask])

    def reset_pnl(self):
        self.df["pnl_buy_qty"] = 0.0
        self.df["pnl_buy_cost"] = 0.0
        self.df["pnl_sell_qty"] = 0.0
        self.df["pnl_sell_revenue"] = 0.0

    def copy(self):
        # Return a copy of the universe DataFrame (for GUI snapshot)
        return self.df.copy()
