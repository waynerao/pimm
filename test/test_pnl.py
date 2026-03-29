# Tests for mark-to-market PnL calculation


class TestPnlAccumulation:
    def test_buy_fill_accumulates(self, state_mgr):
        state_mgr.df.at["0005.HK", "pnl_buy_qty"] += 400.0
        state_mgr.df.at["0005.HK", "pnl_buy_cost"] += 400.0 * 59.0

        assert state_mgr.df.at["0005.HK", "pnl_buy_qty"] == 400.0
        assert state_mgr.df.at["0005.HK", "pnl_buy_cost"] == 23600.0

    def test_sell_fill_accumulates(self, state_mgr):
        state_mgr.df.at["0005.HK", "pnl_sell_qty"] += 200.0
        state_mgr.df.at["0005.HK", "pnl_sell_revenue"] += 200.0 * 61.0

        assert state_mgr.df.at["0005.HK", "pnl_sell_qty"] == 200.0
        assert state_mgr.df.at["0005.HK", "pnl_sell_revenue"] == 12200.0

    def test_pnl_formula_buy_only(self, state_mgr, price_df):
        state_mgr.update_live_price(price_df)
        # Bought 400 @ 59, last_price = 60
        state_mgr.df.at["0005.HK", "pnl_buy_qty"] = 400.0
        state_mgr.df.at["0005.HK", "pnl_buy_cost"] = 400.0 * 59.0

        r = state_mgr.df.loc["0005.HK"]
        pnl = r["last_price"] * (r["pnl_buy_qty"] - r["pnl_sell_qty"]) - r["pnl_buy_cost"] + r["pnl_sell_revenue"]
        # 60 * 400 - 23600 = 24000 - 23600 = 400
        assert pnl == 400.0

    def test_pnl_formula_sell_only(self, state_mgr, price_df):
        state_mgr.update_live_price(price_df)
        # Sold 200 @ 61, last_price = 60
        state_mgr.df.at["0005.HK", "pnl_sell_qty"] = 200.0
        state_mgr.df.at["0005.HK", "pnl_sell_revenue"] = 200.0 * 61.0

        r = state_mgr.df.loc["0005.HK"]
        pnl = r["last_price"] * (r["pnl_buy_qty"] - r["pnl_sell_qty"]) - r["pnl_buy_cost"] + r["pnl_sell_revenue"]
        # 60 * (0 - 200) - 0 + 12200 = -12000 + 12200 = 200
        assert pnl == 200.0

    def test_pnl_formula_buy_and_sell(self, state_mgr, price_df):
        state_mgr.update_live_price(price_df)
        # Bought 400 @ 59, Sold 200 @ 61, last_price = 60
        state_mgr.df.at["0005.HK", "pnl_buy_qty"] = 400.0
        state_mgr.df.at["0005.HK", "pnl_buy_cost"] = 400.0 * 59.0
        state_mgr.df.at["0005.HK", "pnl_sell_qty"] = 200.0
        state_mgr.df.at["0005.HK", "pnl_sell_revenue"] = 200.0 * 61.0

        r = state_mgr.df.loc["0005.HK"]
        pnl = r["last_price"] * (r["pnl_buy_qty"] - r["pnl_sell_qty"]) - r["pnl_buy_cost"] + r["pnl_sell_revenue"]
        # 60 * 200 - 23600 + 12200 = 12000 - 23600 + 12200 = 600
        assert pnl == 600.0


class TestPnlPriceChange:
    def test_pnl_recomputes_with_new_price(self, state_mgr, price_df):
        state_mgr.update_live_price(price_df)
        state_mgr.df.at["0005.HK", "pnl_buy_qty"] = 400.0
        state_mgr.df.at["0005.HK", "pnl_buy_cost"] = 400.0 * 59.0

        # At last_price=60: pnl = 400
        r = state_mgr.df.loc["0005.HK"]
        pnl1 = r["last_price"] * (r["pnl_buy_qty"] - r["pnl_sell_qty"]) - r["pnl_buy_cost"] + r["pnl_sell_revenue"]
        assert pnl1 == 400.0

        # Price goes to 62: pnl = 62*400 - 23600 = 1200
        state_mgr.df.at["0005.HK", "last_price"] = 62.0
        r = state_mgr.df.loc["0005.HK"]
        pnl2 = r["last_price"] * (r["pnl_buy_qty"] - r["pnl_sell_qty"]) - r["pnl_buy_cost"] + r["pnl_sell_revenue"]
        assert pnl2 == 1200.0


class TestPnlReset:
    def test_reset_pnl_zeros_accumulators(self, state_mgr):
        state_mgr.df.at["0005.HK", "pnl_buy_qty"] = 400.0
        state_mgr.df.at["0005.HK", "pnl_buy_cost"] = 23600.0
        state_mgr.df.at["0005.HK", "pnl_sell_qty"] = 200.0
        state_mgr.df.at["0005.HK", "pnl_sell_revenue"] = 12200.0

        state_mgr.reset_pnl()

        assert state_mgr.df.at["0005.HK", "pnl_buy_qty"] == 0.0
        assert state_mgr.df.at["0005.HK", "pnl_buy_cost"] == 0.0
        assert state_mgr.df.at["0005.HK", "pnl_sell_qty"] == 0.0
        assert state_mgr.df.at["0005.HK", "pnl_sell_revenue"] == 0.0

    def test_reset_zeros_all_rics(self, state_mgr):
        for ric in ["0005.HK", "0700.HK"]:
            state_mgr.df.at[ric, "pnl_buy_qty"] = 100.0
            state_mgr.df.at[ric, "pnl_buy_cost"] = 5000.0

        state_mgr.reset_pnl()

        for ric in ["0005.HK", "0700.HK"]:
            assert state_mgr.df.at[ric, "pnl_buy_qty"] == 0.0
            assert state_mgr.df.at[ric, "pnl_buy_cost"] == 0.0


class TestPnlAggregate:
    def test_aggregate_pnl_across_stocks(self, state_mgr, price_df):
        state_mgr.update_live_price(price_df)

        # 0005.HK: bought 400 @ 59, last=60 -> pnl = 400
        state_mgr.df.at["0005.HK", "pnl_buy_qty"] = 400.0
        state_mgr.df.at["0005.HK", "pnl_buy_cost"] = 400.0 * 59.0

        # 0700.HK: sold 100 @ 385, last=380 -> pnl = 380*(0-100) + 38500 = 500
        state_mgr.df.at["0700.HK", "pnl_sell_qty"] = 100.0
        state_mgr.df.at["0700.HK", "pnl_sell_revenue"] = 100.0 * 385.0

        df = state_mgr.df
        pnl_local = (df["last_price"] * (df["pnl_buy_qty"] - df["pnl_sell_qty"])
                     - df["pnl_buy_cost"] + df["pnl_sell_revenue"])
        assert pnl_local.sum() == 900.0

    def test_aggregate_usd_pnl(self, state_mgr, risk_df, price_df):
        state_mgr.update_risk_appetite(risk_df)
        state_mgr.update_live_price(price_df)

        state_mgr.df.at["0005.HK", "pnl_buy_qty"] = 400.0
        state_mgr.df.at["0005.HK", "pnl_buy_cost"] = 400.0 * 59.0

        df = state_mgr.df
        pnl_local = (df["last_price"] * (df["pnl_buy_qty"] - df["pnl_sell_qty"])
                     - df["pnl_buy_cost"] + df["pnl_sell_revenue"])
        pnl_usd = pnl_local * df["fx_rate"]
        # pnl_local for 0005.HK = 400, fx_rate = 0.128 -> usd = 51.2
        assert abs(pnl_usd.sum() - 51.2) < 0.01
