# Config loader using configparser + universe CSV loader

import configparser
import csv
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


class MarketConfig:

    def __init__(self, name, sessions, order_valid_time,
                 refresh_buffer, full_batch_interval,
                 min_dispatch_interval, single_name_cap,
                 max_buy_notional, max_sell_notional,
                 max_staleness, partial_change_threshold=0.10,
                 refill_fill_threshold=0.50, universe_file=None,
                 stock_limit_overrides=None, alpha_enabled=False):
        self.name = name
        self.sessions = sessions
        self.order_valid_time = order_valid_time
        self.refresh_buffer = refresh_buffer
        self.full_batch_interval = full_batch_interval
        self.min_dispatch_interval = min_dispatch_interval
        self.single_name_cap = single_name_cap
        self.max_buy_notional = max_buy_notional
        self.max_sell_notional = max_sell_notional
        self.max_staleness = max_staleness
        self.partial_change_threshold = partial_change_threshold
        self.refill_fill_threshold = refill_fill_threshold
        self.universe_file = universe_file
        self.stock_limit_overrides = stock_limit_overrides or {}
        self.alpha_enabled = alpha_enabled

    def get_stock_limit(self, ric):
        return self.stock_limit_overrides.get(
            ric, self.single_name_cap
        )


def load_config(path, market_name):
    path = Path(path)
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(str(path))
    sec = parser[market_name]

    sessions = [
        SessionWindow.parse(s.strip())
        for s in sec.get("sessions", "").split(",") if s.strip()
    ]

    overrides = {}
    ov_sec = f"{market_name}.overrides"
    if parser.has_section(ov_sec):
        for ric, val in parser.items(ov_sec):
            overrides[ric] = float(val)

    g = sec.get
    gb = sec.getboolean
    return MarketConfig(
        name=market_name, sessions=sessions,
        order_valid_time=int(g("order_valid_time", "5")),
        refresh_buffer=int(g("refresh_buffer", "15")),
        full_batch_interval=int(g("full_batch_interval", "10")),
        min_dispatch_interval=int(g("min_dispatch_interval", "5")),
        single_name_cap=float(g("single_name_cap", "50000")),
        max_buy_notional=float(g("max_buy_notional", "10000000")),
        max_sell_notional=float(g("max_sell_notional", "10000000")),
        max_staleness=int(g("max_staleness", "30")),
        partial_change_threshold=float(
            g("partial_change_threshold", "0.10")),
        refill_fill_threshold=float(
            g("refill_fill_threshold", "0.50")),
        universe_file=g("universe_file"),
        stock_limit_overrides=overrides,
        alpha_enabled=gb("alpha_enabled", fallback=False),
    )


def load_all_markets(path):
    path = Path(path)
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(str(path))
    configs = {}
    skip = {"web", "DEFAULT"}
    for name in parser.sections():
        if "." in name or name in skip:
            continue
        configs[name] = load_config(path, name)
    return configs


def reload_market_config(path, market_name):
    return load_config(path, market_name)


class WebConfig:

    def __init__(self, port=8080, recipients=None,
                 delta_beta_interval=5):
        self.port = port
        self.recipients = recipients or []
        self.delta_beta_interval = delta_beta_interval


def load_web_config(path):
    path = Path(path)
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(str(path))
    if not parser.has_section("web"):
        return WebConfig()
    sec = parser["web"]
    recipients = [
        r.strip()
        for r in sec.get("recipients", "").split(",") if r.strip()
    ]
    return WebConfig(
        port=int(sec.get("port", "8080")),
        recipients=recipients,
        delta_beta_interval=int(sec.get("delta_beta_interval", "5")),
    )


def load_universe(csv_path):
    ric_list = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            ric = row["ric"].strip()
            if ric:
                ric_list.append(ric)
    return ric_list
