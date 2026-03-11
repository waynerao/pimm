# Config loader using configparser + universe CSV loader

import configparser
import csv
from pathlib import Path


class SessionWindow:

    def __init__(self, start_hour, start_minute, end_hour, end_minute):
        self.start_hour = start_hour
        self.start_minute = start_minute
        self.end_hour = end_hour
        self.end_minute = end_minute

    @classmethod
    def parse(cls, raw):
        start, end = raw.split("-")
        sh, sm = (int(x) for x in start.strip().split(":"))
        eh, em = (int(x) for x in end.strip().split(":"))
        return cls(sh, sm, eh, em)


class MarketConfig:

    def __init__(
        self,
        name,
        timezone,
        sessions,
        order_valid_time,
        refresh_buffer,
        full_batch_interval,
        min_dispatch_interval,
        single_name_cap,
        max_buy_notional,
        max_sell_notional,
        max_staleness,
        partial_change_threshold=0.10,
        refill_fill_threshold=0.50,
        universe_file=None,
        stock_limit_overrides=None,
    ):
        self.name = name
        self.timezone = timezone
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

    def get_stock_limit(self, ric):
        return self.stock_limit_overrides.get(ric, self.single_name_cap)


def load_config(path, market_name):
    path = Path(path)
    parser = configparser.ConfigParser()
    parser.optionxform = str  # preserve key case
    parser.read(str(path))

    section = parser[market_name]

    raw_sessions = section.get("sessions", "")
    sessions = [
        SessionWindow.parse(s.strip())
        for s in raw_sessions.split(",") if s.strip()
    ]

    overrides = {}
    override_section = "%s.overrides" % market_name
    if parser.has_section(override_section):
        for ric, val in parser.items(override_section):
            overrides[ric] = float(val)

    return MarketConfig(
        name=market_name,
        timezone=section.get("timezone", "Asia/Hong_Kong"),
        sessions=sessions,
        order_valid_time=int(section.get("order_valid_time", "5")),
        refresh_buffer=int(section.get("refresh_buffer", "15")),
        full_batch_interval=int(section.get("full_batch_interval", "10")),
        min_dispatch_interval=int(section.get("min_dispatch_interval", "5")),
        single_name_cap=float(section.get("single_name_cap", "50000")),
        max_buy_notional=float(section.get("max_buy_notional", "10000000")),
        max_sell_notional=float(section.get("max_sell_notional", "10000000")),
        max_staleness=int(section.get("max_staleness", "30")),
        partial_change_threshold=float(section.get("partial_change_threshold", "0.10")),
        refill_fill_threshold=float(section.get("refill_fill_threshold", "0.50")),
        universe_file=section.get("universe_file"),
        stock_limit_overrides=overrides,
    )


def load_all_markets(path):
    path = Path(path)
    parser = configparser.ConfigParser()
    parser.optionxform = str  # preserve key case
    parser.read(str(path))

    configs = {}
    for section_name in parser.sections():
        if "." in section_name:
            continue
        configs[section_name] = load_config(path, section_name)
    return configs


def reload_market_config(path, market_name):
    return load_config(path, market_name)


def load_universe(csv_path):
    ric_list = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ric = row["ric"].strip()
            if ric:
                ric_list.append(ric)
    return ric_list
