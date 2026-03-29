# Config loader using tomllib (stdlib) + universe CSV loader

import csv
import tomllib
from pathlib import Path


class SessionWindow:
    def __init__(self, sh, sm, eh, em):
        self.start_hour, self.start_minute = sh, sm
        self.end_hour, self.end_minute = eh, em

    @classmethod
    def parse(cls, raw):
        start, end = raw.split("-")
        sh, sm = (int(x) for x in start.strip().split(":"))
        eh, em = (int(x) for x in end.strip().split(":"))
        return cls(sh, sm, eh, em)


class PimmConfig:
    def __init__(self, web_port=8080, max_staleness_s=30, full_batch_interval_m=10, min_dispatch_interval_s=5,
                 delta_beta_interval_s=5, recipients=None):
        self.web_port = web_port
        self.max_staleness_s = max_staleness_s
        self.full_batch_interval_m = full_batch_interval_m
        self.min_dispatch_interval_s = min_dispatch_interval_s
        self.delta_beta_interval_s = delta_beta_interval_s
        self.recipients = recipients or []


class MarketConfig:
    def __init__(self, name, sessions, order_valid_time_m=5, refresh_buffer_s=15, single_name_cap=50000,
                 max_buy_notional=10_000_000, max_sell_notional=10_000_000, partial_change_threshold=0.10,
                 refill_fill_threshold=0.50, universe_file=None, stock_limit_overrides=None, alpha_enabled=False):
        self.name = name
        self.sessions = sessions
        self.order_valid_time_m = order_valid_time_m
        self.refresh_buffer_s = refresh_buffer_s
        self.single_name_cap = single_name_cap
        self.max_buy_notional = max_buy_notional
        self.max_sell_notional = max_sell_notional
        self.partial_change_threshold = partial_change_threshold
        self.refill_fill_threshold = refill_fill_threshold
        self.universe_file = universe_file
        self.stock_limit_overrides = stock_limit_overrides or {}
        self.alpha_enabled = alpha_enabled

    def get_stock_limit(self, ric):
        return self.stock_limit_overrides.get(ric, self.single_name_cap)


def _load_toml(path):
    with open(Path(path), "rb") as f:
        return tomllib.load(f)


def load_pimm_config(path):
    sec = _load_toml(path).get("pimm", {})
    return PimmConfig(
        web_port=sec.get("web_port", 8080), max_staleness_s=sec.get("max_staleness_s", 30),
        full_batch_interval_m=sec.get("full_batch_interval_m", 10),
        min_dispatch_interval_s=sec.get("min_dispatch_interval_s", 5),
        delta_beta_interval_s=sec.get("delta_beta_interval_s", 5), recipients=sec.get("recipients", []))


def load_market_config(path, market_name):
    data = _load_toml(path)
    defaults = data.get("market_defaults", {})
    market_data = data.get("market", {}).get(market_name, {})
    merged = {**defaults, **{k: v for k, v in market_data.items() if k != "overrides"}}
    sessions = [SessionWindow.parse(s) for s in merged.get("sessions", [])]
    overrides = {k: float(v) for k, v in market_data.get("overrides", {}).items()}
    return MarketConfig(
        name=market_name, sessions=sessions, order_valid_time_m=merged.get("order_valid_time_m", 5),
        refresh_buffer_s=merged.get("refresh_buffer_s", 15), single_name_cap=merged.get("single_name_cap", 50000),
        max_buy_notional=merged.get("max_buy_notional", 10_000_000),
        max_sell_notional=merged.get("max_sell_notional", 10_000_000),
        partial_change_threshold=merged.get("partial_change_threshold", 0.10),
        refill_fill_threshold=merged.get("refill_fill_threshold", 0.50),
        universe_file=merged.get("universe_file"), stock_limit_overrides=overrides,
        alpha_enabled=merged.get("alpha_enabled", False))


def load_all_markets(path):
    data = _load_toml(path)
    configs = {}
    for name in data.get("market", {}):
        configs[name] = load_market_config(path, name)
    return configs


def load_universe(csv_path):
    ric_list = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            ric = row["ric"].strip()
            if ric:
                ric_list.append(ric)
    return ric_list
