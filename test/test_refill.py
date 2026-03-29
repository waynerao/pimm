# Tests for refill trigger and capping logic

import pandas as pd

from pimm.engine.refill import accumulate_fills, cap_refill_qty, get_refill_mask, reset_fill_counters


def _make_universe():
    # Helper: 2-stock universe DataFrame with live quantities
    df = pd.DataFrame(
        {
            "lot_size": [400, 100],
            "stock_limit": [50000.0, 50000.0],
            "buy_state": ["best_bid", "best_bid"],
            "sell_state": ["best_ask", "best_ask"],
            "buy_raw": [1000.0, 500.0],
            "sell_raw": [2000.0, 800.0],
            "last_price": [60.0, 380.0],
            "fx_rate": [0.128, 0.128],
            "alpha": [0.0, 0.0],
            "inventory": [5000.0, 1000.0],
            "live_buy_qty": [800.0, 500.0],
            "live_sell_qty": [1600.0, 800.0],
            "last_sent_time": pd.NaT,
            "filled_buy_since_dispatch": [0.0, 0.0],
            "filled_sell_since_dispatch": [0.0, 0.0],
        },
        index=pd.Index(["0005.HK", "0700.HK"], name="ric"),
    )
    return df


class TestGetRefillMask:
    def test_no_fills_no_refill(self):
        df = _make_universe()
        mask = get_refill_mask(df, threshold=0.50)
        assert not mask.any()

    def test_fills_below_threshold(self):
        df = _make_universe()
        df.at["0005.HK", "filled_buy_since_dispatch"] = 300.0  # 300/800 = 37.5%
        mask = get_refill_mask(df, threshold=0.50)
        assert not mask.at["0005.HK"]

    def test_fills_above_threshold(self):
        df = _make_universe()
        df.at["0005.HK", "filled_buy_since_dispatch"] = 500.0  # 500/800 = 62.5%
        mask = get_refill_mask(df, threshold=0.50)
        assert mask.at["0005.HK"]
        assert not mask.at["0700.HK"]

    def test_sell_side_trigger(self):
        df = _make_universe()
        df.at["0700.HK", "filled_sell_since_dispatch"] = 500.0  # 500/800 = 62.5%
        mask = get_refill_mask(df, threshold=0.50)
        assert mask.at["0700.HK"]


class TestCapRefillQty:
    def test_cap_at_optimal_minus_filled(self):
        df = _make_universe()
        df.at["0005.HK", "filled_buy_since_dispatch"] = 600.0

        dispatch = pd.DataFrame(
            {"buy_dispatch": [1200.0, 500.0], "sell_dispatch": [1600.0, 800.0]},
            index=pd.Index(["0005.HK", "0700.HK"], name="ric"),
        )

        capped = cap_refill_qty(dispatch, df)
        # buy_dispatch capped at 1200 - 600 = 600
        assert capped.at["0005.HK", "buy_dispatch"] == 600.0
        # 0700.HK unaffected (0 fills)
        assert capped.at["0700.HK", "buy_dispatch"] == 500.0


class TestResetFillCounters:
    def test_reset_zeros_out(self):
        df = _make_universe()
        df.at["0005.HK", "filled_buy_since_dispatch"] = 500.0
        df.at["0005.HK", "filled_sell_since_dispatch"] = 300.0
        reset_fill_counters(df)
        assert df.at["0005.HK", "filled_buy_since_dispatch"] == 0.0
        assert df.at["0005.HK", "filled_sell_since_dispatch"] == 0.0


class TestAccumulateFills:
    def test_accumulate_increments(self):
        df = _make_universe()
        fills = pd.DataFrame(
            {
                "ric": ["0005.HK", "0005.HK"],
                "side": ["buy", "sell"],
                "fill_qty": [200.0, 300.0],
                "fill_price": [60.0, 60.5],
                "timestamp": pd.Timestamp.now(),
            }
        )
        accumulate_fills(df, fills)
        assert df.at["0005.HK", "filled_buy_since_dispatch"] == 200.0
        assert df.at["0005.HK", "filled_sell_since_dispatch"] == 300.0

    def test_accumulate_stacks(self):
        df = _make_universe()
        df.at["0005.HK", "filled_buy_since_dispatch"] = 100.0
        fills = pd.DataFrame(
            {
                "ric": ["0005.HK"],
                "side": ["buy"],
                "fill_qty": [200.0],
                "fill_price": [60.0],
                "timestamp": pd.Timestamp.now(),
            }
        )
        accumulate_fills(df, fills)
        assert df.at["0005.HK", "filled_buy_since_dispatch"] == 300.0
