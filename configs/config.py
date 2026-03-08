import csv
import tomllib
from pathlib import Path


class SessionWindow:
    # A trading session time window (HH:MM-HH:MM)

    def __init__(self, start_hour, start_minute, end_hour, end_minute):
        self.start_hour = start_hour
        self.start_minute = start_minute
        self.end_hour = end_hour
        self.end_minute = end_minute

    @classmethod
    def parse(cls, raw):
        # Parse "HH:MM-HH:MM" into a SessionWindow
        start, end = raw.split("-")
        sh, sm = (int(x) for x in start.strip().split(":"))
        eh, em = (int(x) for x in end.strip().split(":"))
        return cls(sh, sm, eh, em)


class MarketConfig:
    # Full resolved configuration for a market

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
        # ric -> stock_limit override
        self.stock_limit_overrides = stock_limit_overrides or {}

    def get_stock_limit(self, ric):
        # Check per-stock override, fall back to market default
        return self.stock_limit_overrides.get(ric, self.single_name_cap)


def load_config(path, market_name):
    # Load a market config from TOML, reading the [market_name] section
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)

    section = raw[market_name]

    sessions = [SessionWindow.parse(s) for s in section["sessions"]]

    # Per-stock limit overrides from [overrides.stock_limit.{market_name}]
    overrides = raw.get("overrides", {}).get("stock_limit", {}).get(market_name, {})

    return MarketConfig(
        name=market_name,
        timezone=section.get("timezone", "Asia/Hong_Kong"),
        sessions=sessions,
        order_valid_time=section["order_valid_time"],
        refresh_buffer=section.get("refresh_buffer", 15),
        full_batch_interval=section.get("full_batch_interval", 10),
        min_dispatch_interval=section.get("min_dispatch_interval", 5),
        single_name_cap=section["single_name_cap"],
        max_buy_notional=section["max_buy_notional"],
        max_sell_notional=section["max_sell_notional"],
        max_staleness=section.get("max_staleness", 30),
        partial_change_threshold=section.get("partial_change_threshold", 0.10),
        refill_fill_threshold=section.get("refill_fill_threshold", 0.50),
        universe_file=section.get("universe_file"),
        stock_limit_overrides=dict(overrides),
    )


def load_universe(csv_path):
    # Load stock universe from CSV, returns list of RIC strings
    ric_list = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ric = row["ric"].strip()
            if ric:
                ric_list.append(ric)
    return ric_list
