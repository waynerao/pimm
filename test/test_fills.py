# Tests for fill accumulation and state feed updates

import pandas as pd

from pimm.engine.refill import accumulate_fills


class TestFillAccumulation:
    def test_accumulate_buy_fills(self, state_mgr, risk_df):
        state_mgr.update_risk_appetite(risk_df)
        fills = pd.DataFrame({
            "ric": ["0005.HK", "0005.HK"],
            "side": ["buy", "buy"],
            "fill_qty": [100.0, 200.0],
            "fill_price": [50.0, 51.0],
            "timestamp": pd.Timestamp.now(),
        })
        accumulate_fills(state_mgr.df, fills)
        assert state_mgr.df.at["0005.HK", "filled_buy_since_dispatch"] == 300.0

    def test_accumulate_sell_fills(self, state_mgr, risk_df):
        state_mgr.update_risk_appetite(risk_df)
        fills = pd.DataFrame({
            "ric": ["0005.HK"],
            "side": ["sell"],
            "fill_qty": [500.0],
            "fill_price": [50.5],
            "timestamp": pd.Timestamp.now(),
        })
        accumulate_fills(state_mgr.df, fills)
        assert state_mgr.df.at["0005.HK", "filled_sell_since_dispatch"] == 500.0

    def test_accumulate_multi_ric(self, state_mgr, risk_df):
        state_mgr.update_risk_appetite(risk_df)
        fills = pd.DataFrame({
            "ric": ["0005.HK", "0700.HK"],
            "side": ["buy", "buy"],
            "fill_qty": [100.0, 200.0],
            "fill_price": [50.0, 350.0],
            "timestamp": pd.Timestamp.now(),
        })
        accumulate_fills(state_mgr.df, fills)
        assert state_mgr.df.at["0005.HK", "filled_buy_since_dispatch"] == 100.0
        assert state_mgr.df.at["0700.HK", "filled_buy_since_dispatch"] == 200.0

    def test_unknown_ric_skipped(self, state_mgr):
        fills = pd.DataFrame({
            "ric": ["UNKNOWN.XX"],
            "side": ["buy"],
            "fill_qty": [100.0],
            "fill_price": [50.0],
            "timestamp": pd.Timestamp.now(),
        })
        # Should not raise
        accumulate_fills(state_mgr.df, fills)


class TestQuoteStatus:
    def test_all_rics_included(self, hk_config):
        from pimm.engine.state import StateManager
        rics = ["0005.HK", "0700.HK", "MISSING.HK"]
        lots = {"0005.HK": 400, "0700.HK": 100}
        mgr = StateManager(rics, lots, hk_config)
        assert len(mgr.df) == 3
        assert "MISSING.HK" in mgr.df.index

    def test_missing_lot_size_sets_status_off(self, hk_config):
        from pimm.engine.state import StateManager
        rics = ["0005.HK", "MISSING.HK"]
        lots = {"0005.HK": 400}
        mgr = StateManager(rics, lots, hk_config)
        assert mgr.df.at["0005.HK", "quote_status"] == True  # noqa: E712
        assert mgr.df.at["MISSING.HK", "quote_status"] == False  # noqa: E712
        assert mgr.df.at["MISSING.HK", "remark"] == "no lot size"

    def test_quotable_excludes_off(self, hk_config):
        from pimm.engine.state import StateManager
        rics = ["0005.HK", "MISSING.HK"]
        lots = {"0005.HK": 400}
        mgr = StateManager(rics, lots, hk_config)
        quotable = mgr.quotable
        assert "0005.HK" in quotable.index
        assert "MISSING.HK" not in quotable.index


class TestStateManagerFeedUpdates:
    def test_risk_appetite_update(self, state_mgr, risk_df):
        state_mgr.update_risk_appetite(risk_df)
        assert state_mgr.df.at["0005.HK", "buy_raw"] == 1000.0
        assert state_mgr.df.at["0005.HK", "sell_raw"] == 2000.0
        assert state_mgr.df.at["0005.HK", "fx_rate"] == 0.128

    def test_live_price_update(self, state_mgr, price_df):
        state_mgr.update_live_price(price_df)
        assert state_mgr.df.at["0005.HK", "last_price"] == 60.0
        assert state_mgr.df.at["0700.HK", "last_price"] == 380.0

    def test_inventory_update(self, state_mgr, risk_df, inventory_df):
        state_mgr.update_risk_appetite(risk_df)
        state_mgr.update_inventory(inventory_df)
        assert state_mgr.df.at["0005.HK", "inventory"] == 5000.0

    def test_alpha_update(self, state_mgr, risk_df, alpha_df):
        state_mgr.update_risk_appetite(risk_df)
        state_mgr.update_alpha(alpha_df)
        assert state_mgr.df.at["0005.HK", "alpha"] == 0.0

    def test_alpha_clamped(self, state_mgr):
        df = pd.DataFrame({"ric": ["0005.HK"], "alpha": [5.0]})
        state_mgr.update_alpha(df)
        assert state_mgr.df.at["0005.HK", "alpha"] == 1.0

    def test_unknown_ric_skipped(self, state_mgr):
        df = pd.DataFrame({
            "ric": ["UNKNOWN.XX"],
            "buy_state": ["best_bid"],
            "buy_qty": [100.0],
            "sell_state": ["best_ask"],
            "sell_qty": [100.0],
            "fx_rate": [1.0],
        })
        state_mgr.update_risk_appetite(df)
        assert "UNKNOWN.XX" not in state_mgr.df.index

    def test_live_price_unknown_ric_skipped(self, state_mgr):
        df = pd.DataFrame({
            "ric": ["UNKNOWN.XX"],
            "last_price": [10.0],
        })
        state_mgr.update_live_price(df)
        assert "UNKNOWN.XX" not in state_mgr.df.index
