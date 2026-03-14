from pathlib import Path

import pandas as pd
import pytest

from pimm.config import MarketConfig, SessionWindow, load_config
from pimm.engine.state import StateManager


@pytest.fixture
def config_path():
    return Path(__file__).parent.parent / "configs" / "config.cfg"


@pytest.fixture
def hk_config(config_path):
    return load_config(config_path, "HK")


@pytest.fixture
def ric_list():
    return ["0005.HK", "0700.HK", "9988.HK", "1299.HK", "0388.HK"]


@pytest.fixture
def lot_sizes():
    return {
        "0005.HK": 400,
        "0700.HK": 100,
        "9988.HK": 100,
        "1299.HK": 500,
        "0388.HK": 100,
    }


@pytest.fixture
def state_mgr(ric_list, lot_sizes, hk_config):
    return StateManager(ric_list, lot_sizes, hk_config)


@pytest.fixture
def risk_df():
    return pd.DataFrame({
        "ric": ["0005.HK", "0700.HK", "9988.HK"],
        "buy_state": ["best_bid", "best_bid", "best_bid"],
        "buy_qty": [1000.0, 500.0, 300.0],
        "sell_state": ["best_ask", "best_ask", "best_ask"],
        "sell_qty": [2000.0, 800.0, 400.0],
        "fx_rate": [0.128, 0.128, 0.128],
    })


@pytest.fixture
def price_df():
    return pd.DataFrame({
        "ric": ["0005.HK", "0700.HK", "9988.HK"],
        "last_price": [60.0, 380.0, 85.0],
    })


@pytest.fixture
def inventory_df():
    return pd.DataFrame({
        "ric": ["0005.HK", "0700.HK", "9988.HK"],
        "inventory": [5000.0, 300.0, 1000.0],
    })


@pytest.fixture
def alpha_df():
    return pd.DataFrame({
        "ric": ["0005.HK", "0700.HK", "9988.HK"],
        "alpha": [0.0, 0.0, 0.0],
    })


@pytest.fixture
def fills_df():
    return pd.DataFrame({
        "ric": ["0005.HK", "0005.HK", "0700.HK"],
        "side": ["buy", "sell", "buy"],
        "fill_qty": [200.0, 500.0, 100.0],
        "fill_price": [50.0, 50.5, 350.0],
        "timestamp": pd.Timestamp.now(),
    })


def make_config(sessions):
    return MarketConfig(
        name="TEST",
        sessions=[SessionWindow.parse(s) for s in sessions],
        order_valid_time=5,
        refresh_buffer=15,
        full_batch_interval=10,
        min_dispatch_interval=5,
        single_name_cap=50000,
        max_buy_notional=10_000_000,
        max_sell_notional=10_000_000,
        max_staleness=30,
        partial_change_threshold=0.10,
        refill_fill_threshold=0.50,
    )
