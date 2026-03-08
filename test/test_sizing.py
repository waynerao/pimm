# Tests for the 4-step vectorized sizing pipeline

import pandas as pd
import pytest

from pimm.engine.sizing import (
    apply_inventory_constraint,
    compute_optimal_cached,
    compute_optimal_quotes,
)


def _make_df(buy_raw, sell_raw, alpha=0.0, stock_limit=50000.0,
             last_price=100.0, fx_rate=1.0, lot_size=100, inventory=10000.0):
    # Helper to build a single-row universe DataFrame
    return pd.DataFrame({
        "buy_raw": [buy_raw],
        "sell_raw": [sell_raw],
        "alpha": [alpha],
        "stock_limit": [stock_limit],
        "last_price": [last_price],
        "fx_rate": [fx_rate],
        "lot_size": [lot_size],
        "inventory": [inventory],
    }, index=pd.Index(["TEST.HK"], name="ric"))


def _make_multi_df():
    # 2-stock DataFrame for scaling tests
    return pd.DataFrame({
        "buy_raw": [600.0, 600.0],
        "sell_raw": [100.0, 100.0],
        "alpha": [0.0, 0.0],
        "stock_limit": [50000.0, 50000.0],
        "last_price": [1.0, 1.0],
        "fx_rate": [1.0, 1.0],
        "lot_size": [100, 100],
        "inventory": [10000.0, 10000.0],
    }, index=pd.Index(["A", "B"], name="ric"))


class TestAlphaSkew:
    def test_neutral_alpha(self):
        df = _make_df(100.0, 200.0, alpha=0.0)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 100.0
        assert result.at["TEST.HK", "sell_optimal"] == 200.0

    def test_positive_alpha_inflates_buy(self):
        df = _make_df(100.0, 200.0, alpha=0.5)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 100.0  # 150 rounded to 100
        assert result.at["TEST.HK", "sell_optimal"] == 100.0  # 100 rounded to 100

    def test_negative_alpha_inflates_sell(self):
        df = _make_df(100.0, 200.0, alpha=-0.5)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 0.0    # 50 rounded to 0
        assert result.at["TEST.HK", "sell_optimal"] == 300.0

    def test_max_positive_alpha(self):
        df = _make_df(100.0, 200.0, alpha=1.0)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 200.0
        assert result.at["TEST.HK", "sell_optimal"] == 0.0

    def test_max_negative_alpha(self):
        df = _make_df(100.0, 200.0, alpha=-1.0)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 0.0
        assert result.at["TEST.HK", "sell_optimal"] == 400.0


class TestSingleNameCap:
    def test_below_cap(self):
        df = _make_df(100.0, 200.0, stock_limit=500.0)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 100.0
        assert result.at["TEST.HK", "sell_optimal"] == 200.0

    def test_above_cap(self):
        df = _make_df(1000.0, 2000.0, stock_limit=500.0)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 500.0
        assert result.at["TEST.HK", "sell_optimal"] == 500.0


class TestNotionalScaling:
    def test_within_limits(self):
        df = _make_multi_df()
        _, buy_s, sell_s = compute_optimal_quotes(df, 10000.0, 10000.0)
        assert buy_s == 1.0
        assert sell_s == 1.0

    def test_buy_exceeds(self):
        df = _make_multi_df()
        # buy_notional: 600 * 1.0 * 1.0 = 600 each, total = 1200
        _, buy_s, sell_s = compute_optimal_quotes(df, 1000.0, 10000.0)
        assert buy_s == pytest.approx(1000.0 / 1200.0)
        assert sell_s == 1.0

    def test_scaling_reduces_qty(self):
        df = _make_multi_df()
        result, buy_s, _ = compute_optimal_quotes(df, 1000.0, 10000.0)
        # 600 * (1000/1200) = 500, lot rounded to 500
        assert result.at["A", "buy_optimal"] == 500.0
        assert result.at["B", "buy_optimal"] == 500.0


class TestLotRounding:
    def test_exact_multiple(self):
        df = _make_df(400.0, 800.0, lot_size=400)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 400.0
        assert result.at["TEST.HK", "sell_optimal"] == 800.0

    def test_rounds_down(self):
        df = _make_df(550.0, 999.0, lot_size=400)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 400.0
        assert result.at["TEST.HK", "sell_optimal"] == 800.0

    def test_below_lot_size(self):
        df = _make_df(50.0, 50.0, lot_size=100)
        result, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        assert result.at["TEST.HK", "buy_optimal"] == 0.0
        assert result.at["TEST.HK", "sell_optimal"] == 0.0


class TestInventoryConstraint:
    def test_sell_within_inventory(self):
        df = _make_df(100.0, 200.0, inventory=500.0)
        optimal, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        dispatch = apply_inventory_constraint(optimal, df)
        assert dispatch.at["TEST.HK", "buy_dispatch"] == 100.0
        assert dispatch.at["TEST.HK", "sell_dispatch"] == 200.0

    def test_sell_exceeds_inventory(self):
        df = _make_df(100.0, 200.0, inventory=50.0)
        optimal, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        dispatch = apply_inventory_constraint(optimal, df)
        assert dispatch.at["TEST.HK", "buy_dispatch"] == 100.0
        assert dispatch.at["TEST.HK", "sell_dispatch"] == 50.0

    def test_zero_inventory(self):
        df = _make_df(100.0, 200.0, inventory=0.0)
        optimal, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        dispatch = apply_inventory_constraint(optimal, df)
        assert dispatch.at["TEST.HK", "sell_dispatch"] == 0.0

    def test_negative_inventory_clamps_to_zero(self):
        df = _make_df(100.0, 200.0, inventory=-10.0)
        optimal, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        dispatch = apply_inventory_constraint(optimal, df)
        assert dispatch.at["TEST.HK", "sell_dispatch"] == 0.0


class TestCachedScaling:
    def test_uses_cached_factors(self):
        df = _make_df(1000.0, 2000.0, lot_size=100)
        result = compute_optimal_cached(df, 0.5, 0.8)
        # 1000 * 0.5 = 500
        assert result.at["TEST.HK", "buy_optimal"] == 500.0
        # 2000 * 0.8 = 1600
        assert result.at["TEST.HK", "sell_optimal"] == 1600.0


class TestFullPipeline:
    def test_pipeline_all_steps(self):
        # alpha=0.3: buy=500*1.3=650, sell=800*0.7=560
        # cap=600: buy=600, sell=560
        # scaling=1.0 (within 1e9 limit): buy=600, sell=560
        # lot=100: buy=600, sell=500
        # inventory=400: sell_dispatch=min(500,400)=400
        df = _make_df(500.0, 800.0, alpha=0.3, stock_limit=600.0,
                      lot_size=100, inventory=400.0)
        optimal, _, _ = compute_optimal_quotes(df, 1e9, 1e9)
        dispatch = apply_inventory_constraint(optimal, df)
        assert dispatch.at["TEST.HK", "buy_dispatch"] == 600.0
        assert dispatch.at["TEST.HK", "sell_dispatch"] == 400.0

    def test_pipeline_with_scaling(self):
        # alpha=0.3: buy=500*1.3=650, sell=800*0.7=560
        # cap=600: buy=600, sell=560
        # Use cached scaling=0.9: buy=540, sell=504
        # lot=100: buy=500, sell=500
        # inventory=400: sell_dispatch=min(500,400)=400
        df = _make_df(500.0, 800.0, alpha=0.3, stock_limit=600.0,
                      lot_size=100, inventory=400.0)
        optimal = compute_optimal_cached(df, 0.9, 0.9)
        dispatch = apply_inventory_constraint(optimal, df)
        assert dispatch.at["TEST.HK", "buy_dispatch"] == 500.0
        assert dispatch.at["TEST.HK", "sell_dispatch"] == 400.0
