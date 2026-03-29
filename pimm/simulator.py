# Simulation harness for end-to-end testing without desktool/alphaflow
#
# Usage:
#   uv run python -m pimm.simulator configs/simulator.toml
#   uv run python -m pimm.simulator configs/simulator.toml --seed 42

import argparse
import asyncio
import logging
import random
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import uvicorn

from pimm.config import load_all_markets, load_pimm_config, load_universe
from pimm.engine.loop import MarketState, TradingEngine
from pimm.engine.state import StateManager
from pimm.feeds.alpha import AlphaFeed
from pimm.feeds.fills import FillsFeed
from pimm.feeds.heartbeat import HeartbeatMonitor
from pimm.feeds.inventory import InventoryFeed
from pimm.feeds.live_price import LivePriceFeed
from pimm.feeds.risk_appetite import RiskAppetiteFeed
from pimm.utils.network import get_host_ip
from pimm.web.server import broadcast_snapshot, create_app, generate_token

logger = logging.getLogger("pimm.simulator")

BUY_PRICE_TYPES = ["best_bid"]
SELL_PRICE_TYPES = ["best_ask"]

# Approximate prices and fx rates per market
STUB_PRICES = {
    # HK (HKD)
    "0005.HK": 60.0,
    "0700.HK": 380.0,
    "9988.HK": 85.0,
    "1299.HK": 140.0,
    "0388.HK": 310.0,
    # CN (CNY)
    "600519.SS": 1700.0,
    "601318.SS": 50.0,
    "000858.SZ": 150.0,
    "600036.SS": 35.0,
    "000333.SZ": 55.0,
    # TW (TWD)
    "2330.TW": 580.0,
    "2317.TW": 105.0,
    "2454.TW": 950.0,
    "2412.TW": 120.0,
    "3711.TW": 280.0,
}
STUB_FX = {"HK": 0.128, "CN": 0.138, "TW": 0.031}
SUFFIX_TO_MARKET = {"HK": "HK", "SS": "CN", "SZ": "CN", "TW": "TW"}

STUB_LOT_SIZES = {
    # HK
    "0005.HK": 400,
    "0700.HK": 100,
    "9988.HK": 100,
    "1299.HK": 500,
    "0388.HK": 100,
    # CN
    "600519.SS": 100,
    "601318.SS": 100,
    "000858.SZ": 100,
    "600036.SS": 100,
    "000333.SZ": 100,
    # TW
    "2330.TW": 1000,
    "2317.TW": 1000,
    "2454.TW": 1000,
    "2412.TW": 1000,
    "3711.TW": 1000,
}


def _get_trading_day_type(market):
    """Stub: always returns 1 (full trading day)."""
    return 1


class WebLogHandler(logging.Handler):
    """Captures log messages for web dashboard."""

    def __init__(self, engine):
        super().__init__(level=logging.INFO)
        self._engine = engine

    def emit(self, record):
        msg = self.format(record)
        self._engine.add_console_log(record.levelname, msg)


def _setup_logging(log_path):
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%H:%M:%S")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(fh)


# -- Simulation threads --


def _sim_risk_appetite(feed, running, rng, all_rics, fx_map):
    while running.is_set():
        rows = []
        for ric in all_rics:
            suffix = ric.split(".")[-1] if "." in ric else "HK"
            market = SUFFIX_TO_MARKET.get(suffix, suffix)
            fx = fx_map.get(market, 0.128)
            rows.append({"ric": ric, "buy_state": rng.choice(BUY_PRICE_TYPES),
                          "buy_qty": rng.randint(1000, 50000), "sell_state": rng.choice(SELL_PRICE_TYPES),
                          "sell_qty": rng.randint(1000, 50000), "fx_rate": fx})
        feed.on_update(pd.DataFrame(rows))
        time.sleep(3)


def _sim_live_price(feed, running, rng, all_rics, live_prices):
    while running.is_set():
        rows = []
        for ric in all_rics:
            base_price = STUB_PRICES.get(ric, 100.0)
            price = base_price * rng.uniform(0.98, 1.02)
            live_prices[ric] = price
            rows.append({"ric": ric, "last_price": price})
        feed.on_update(pd.DataFrame(rows))
        time.sleep(2)


def _sim_inventory(feed, running, rng, all_rics):
    while running.is_set():
        feed.on_update(pd.DataFrame([{"ric": ric, "inventory": rng.randint(0, 20000)} for ric in all_rics]))
        time.sleep(5)


def _sim_alpha(feed, running, rng, rics):
    while running.is_set():
        feed.on_update(pd.DataFrame([{"ric": ric, "alpha": round(rng.uniform(-0.3, 0.3), 4)} for ric in rics]))
        time.sleep(20)


def _sim_fills(feed, running, rng, live_quotes, live_prices, lot_sizes):
    """Generate fills against live quotes."""
    while running.is_set():
        quotes = dict(live_quotes)
        if not quotes:
            time.sleep(1)
            continue

        rics = list(quotes.keys())
        n = rng.randint(1, min(3, len(rics)))
        chosen = rng.sample(rics, k=n)

        rows = []
        for ric in chosen:
            q = quotes.get(ric)
            if q is None:
                continue
            buy_qty, sell_qty = q["buy_qty"], q["sell_qty"]
            if buy_qty <= 0 and sell_qty <= 0:
                continue

            sides = []
            if buy_qty > 0:
                sides.append("buy")
            if sell_qty > 0:
                sides.append("sell")
            side = rng.choice(sides)

            avail = buy_qty if side == "buy" else sell_qty
            lot = lot_sizes.get(ric, 100)
            max_lots = max(1, int(avail / lot) // 2)
            fill_qty = float(rng.randint(1, max_lots) * lot)

            base_price = live_prices.get(ric, STUB_PRICES.get(ric, 100.0))
            slip = rng.uniform(-0.002, 0.002)
            fill_price = round(base_price * (1 + slip), 4)

            rows.append({"ric": ric, "side": side, "fill_qty": fill_qty, "fill_price": fill_price,
                          "timestamp": pd.Timestamp.now(tz="Asia/Hong_Kong")})

            if side == "buy":
                q["buy_qty"] = max(0, buy_qty - fill_qty)
            else:
                q["sell_qty"] = max(0, sell_qty - fill_qty)

        if rows:
            feed.on_update(pd.DataFrame(rows))
        time.sleep(rng.uniform(2, 6))


# -- Main --


def main():
    parser = argparse.ArgumentParser(description="pimm simulator — end-to-end testing harness")
    parser.add_argument("config", help="Path to TOML config file (e.g. configs/simulator.toml)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    parser.add_argument("--port", type=int, default=None, help="Web server port override")
    args = parser.parse_args()

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    log_path = Path("logs") / f"sim_{ts}.log"
    _setup_logging(log_path)

    logger.info("=== pimm simulator starting ===")

    pimm_config = load_pimm_config(args.config)
    all_configs = load_all_markets(args.config)
    port = args.port or pimm_config.web_port
    logger.info(f"Loaded {len(all_configs)} markets: {list(all_configs.keys())}")

    # Build per-market state with simulator overrides
    market_states = {}
    all_rics = []
    for mkt, config in all_configs.items():
        ric_list = load_universe(config.universe_file)
        state_mgr = StateManager(ric_list, config)
        day_type = _get_trading_day_type(mkt)
        market_states[mkt] = MarketState(mkt, config, state_mgr, day_type=day_type)
        all_rics.extend(ric_list)
        logger.info(f"[{mkt}] day_type={day_type}")

    heartbeat = HeartbeatMonitor(max_staleness_s=pimm_config.max_staleness_s)

    # Shared state for fill simulator
    live_quotes = {}  # ric -> {buy_qty, sell_qty}
    live_prices = {}  # ric -> float

    def dispatch(df):
        logger.info(f"DISPATCH to KDB+:\n{df.to_string(index=False)}")
        for _, row in df.iterrows():
            ric = str(row["ric"])
            buy_qty = float(row["buy_qty"])
            sell_qty = float(row["sell_qty"])
            if buy_qty > 0 or sell_qty > 0:
                live_quotes[ric] = {"buy_qty": buy_qty, "sell_qty": sell_qty}
            else:
                live_quotes.pop(ric, None)

    # Feed adapters
    loop = asyncio.new_event_loop()

    def push_event(event_type, data):
        loop.call_soon_threadsafe(lambda: engine.event_queue.put_nowait((event_type, data)))

    risk_feed = RiskAppetiteFeed(engine_push=push_event)
    price_feed = LivePriceFeed(engine_push=push_event)
    fills_feed = FillsFeed(engine_push=push_event)

    # Per-market feeds stored as dicts
    inventory_feeds = {}
    alpha_feeds = {}
    for mkt, ms in market_states.items():
        rics = list(ms.state_mgr.df.index)
        inventory_feeds[mkt] = InventoryFeed(engine_push=push_event, ric_list=rics, market_name=mkt)
        if ms.config.alpha_enabled:
            alpha_feeds[mkt] = AlphaFeed(engine_push=push_event, ric_list=rics, market_name=mkt)

    # Web server
    token = generate_token()
    engine_ref = {}
    app = create_app(engine_ref, token)

    async def snapshot_cb(snap):
        await broadcast_snapshot(app, snap)

    engine = TradingEngine(
        market_states=market_states, config_path=args.config, dispatch_callback=dispatch,
        snapshot_callback=snapshot_cb, inventory_feeds=inventory_feeds, alpha_feeds=alpha_feeds)

    engine_ref["cmd_callback"] = lambda action, market: engine.process_web_command(action, market)

    web_handler = WebLogHandler(engine)
    web_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(web_handler)

    # Start shared feeds
    heartbeat.start()
    risk_feed.start(loop)
    price_feed.start(loop)
    fills_feed.start(loop)

    # Per-market feeds start/stop with session (managed by engine's _session_monitor)

    # Simulation control
    running = threading.Event()
    running.set()
    rng = random.Random(args.seed)

    sim_threads = [
        threading.Thread(target=_sim_risk_appetite, args=(risk_feed, running, rng, all_rics, STUB_FX),
                         name="sim-risk", daemon=True),
        threading.Thread(target=_sim_live_price, args=(price_feed, running, rng, all_rics, live_prices),
                         name="sim-price", daemon=True),
        threading.Thread(
            target=_sim_fills, args=(fills_feed, running, rng, live_quotes, live_prices, STUB_LOT_SIZES),
            name="sim-fills", daemon=True),
    ]

    # Per-market sim threads: inventory + alpha
    for mkt, inv in inventory_feeds.items():
        rics = list(market_states[mkt].state_mgr.df.index)
        sim_threads.append(threading.Thread(
            target=_sim_inventory, args=(inv, running, rng, rics), name=f"sim-inventory-{mkt}", daemon=True))

    for mkt, alpha in alpha_feeds.items():
        rics = list(market_states[mkt].state_mgr.df.index)
        sim_threads.append(threading.Thread(
            target=_sim_alpha, args=(alpha, running, rng, rics), name=f"sim-alpha-{mkt}", daemon=True))

    def handle_shutdown(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        running.clear()
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(engine.shutdown()))

    signal.signal(signal.SIGINT, handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown)

    for t in sim_threads:
        t.start()
    logger.info(f"Simulation threads started (seed={args.seed})")

    url = f"http://{get_host_ip()}:{port}/?token={token}"
    logger.info(f"Web dashboard: {url}")
    print(f"\n  Dashboard URL: {url}\n")

    async def run_all():
        uvi_config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning", loop="none")
        server = uvicorn.Server(uvi_config)
        server.install_signal_handlers = lambda: None

        engine_task = asyncio.create_task(engine.run())
        server_task = asyncio.create_task(server.serve())

        done, pending = await asyncio.wait([engine_task, server_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

    try:
        loop.run_until_complete(run_all())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt, shutting down...")
        running.clear()
        loop.run_until_complete(engine.shutdown())
    finally:
        running.clear()
        heartbeat.stop()
        risk_feed.stop()
        price_feed.stop()
        fills_feed.stop()
        for inv in inventory_feeds.values():
            inv.stop()
        for alpha in alpha_feeds.values():
            alpha.stop()
        for t in sim_threads:
            t.join(timeout=2)
        loop.close()
        logger.info(f"=== pimm simulator stopped === (log: {log_path})")


if __name__ == "__main__":
    main()
