# Simulation harness for end-to-end testing without desktool/alphaflow
#
# Usage:
#   uv run python -m pimm.simulator configs/config.toml --market HK
#   uv run python -m pimm.simulator configs/config.toml --market HK --gui
#   uv run python -m pimm.simulator configs/config.toml --market HK --seed 42

import argparse
import asyncio
import logging
import random
import signal
import threading
import time
from datetime import UTC, datetime
from multiprocessing import Queue
from pathlib import Path

import pandas as pd

from configs.config import SessionWindow, load_config, load_universe
from pimm.engine.loop import TradingEngine
from pimm.engine.state import StateManager
from pimm.feeds.alpha import AlphaFeed
from pimm.feeds.fills import FillsFeed
from pimm.feeds.heartbeat import HeartbeatMonitor
from pimm.feeds.inventory import InventoryFeed
from pimm.feeds.live_price import LivePriceFeed
from pimm.feeds.risk_appetite import RiskAppetiteFeed
from pimm.gui.process import start_gui_process
from pimm.utils.lots import build_lot_size_table

logger = logging.getLogger("pimm.simulator")

PRICE_TYPES = ["best_bid", "mid", "best_offer"]

# Approximate HKD prices and HKD->USD fx rate
STUB_PRICES = {
    "0005.HK": 60.0,
    "0700.HK": 380.0,
    "9988.HK": 85.0,
    "1299.HK": 140.0,
    "0388.HK": 310.0,
}
HKD_USD_FX = 0.128


def _get_stub_lot_sizes(ric_list):
    stub = {
        "0005.HK": 400, "0700.HK": 100, "9988.HK": 100,
        "1299.HK": 500, "0388.HK": 100,
    }
    rows = [{"ric": r, "lot_size": stub.get(r, 100)} for r in ric_list]
    return pd.DataFrame(rows)


def _setup_logging(log_path=None):
    handlers = [logging.StreamHandler()]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(str(log_path), encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


# -- Simulation threads --


def _sim_risk_appetite(feed, running, rng, rics):
    while running.is_set():
        rows = []
        for ric in rics:
            rows.append({
                "ric": ric,
                "buy_state": rng.choice(PRICE_TYPES),
                "buy_qty": rng.randint(1000, 50000),
                "sell_state": rng.choice(PRICE_TYPES),
                "sell_qty": rng.randint(1000, 50000),
                "fx_rate": HKD_USD_FX,
            })
        df = pd.DataFrame(rows)
        logger.info("SIM risk_appetite:\n%s", df.to_string(index=False))
        feed.on_update(df)
        time.sleep(3)


def _sim_live_price(feed, running, rng, rics):
    while running.is_set():
        rows = []
        for ric in rics:
            base_price = STUB_PRICES.get(ric, 100.0)
            rows.append({
                "ric": ric,
                "last_price": base_price * rng.uniform(0.98, 1.02),
            })
        df = pd.DataFrame(rows)
        logger.info("SIM live_price:\n%s", df.to_string(index=False))
        feed.on_update(df)
        time.sleep(2)


def _sim_inventory(feed, running, rng, rics):
    while running.is_set():
        rows = [{"ric": ric, "inventory": rng.randint(0, 20000)} for ric in rics]
        df = pd.DataFrame(rows)
        logger.info("SIM inventory:\n%s", df.to_string(index=False))
        feed.on_update(df)
        time.sleep(5)


def _sim_alpha(feed, running, rng, rics):
    while running.is_set():
        rows = [
            {"ric": ric, "alpha": round(rng.uniform(-0.3, 0.3), 4)} for ric in rics
        ]
        df = pd.DataFrame(rows)
        logger.info("SIM alpha:\n%s", df.to_string(index=False))
        feed.on_update(df)
        time.sleep(20)


def _sim_fills(feed, running, rng, rics):
    while running.is_set():
        n = rng.randint(1, 4)
        chosen = rng.sample(rics, k=min(n, len(rics)))
        rows = []
        for ric in chosen:
            rows.append({
                "ric": ric,
                "side": rng.choice(["buy", "sell"]),
                "fill_qty": float(rng.randint(100, 5000)),
                "fill_price": round(rng.uniform(50, 500), 2),
                "timestamp": pd.Timestamp.now(tz="Asia/Hong_Kong"),
            })
        df = pd.DataFrame(rows)
        logger.info("SIM fills:\n%s", df.to_string(index=False))
        feed.on_update(df)
        time.sleep(4)


# -- Main --


def main():
    parser = argparse.ArgumentParser(
        description="pimm simulator — end-to-end testing harness"
    )
    parser.add_argument("config", help="Path to market TOML config file")
    parser.add_argument("--market", default="HK", help="Market section name")
    parser.add_argument("--gui", action="store_true", help="Launch the GUI dashboard")
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    args = parser.parse_args()

    # Log file
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    log_path = Path("logs") / ("sim_%s.log" % ts)
    _setup_logging(log_path)

    logger.info("=== pimm simulator starting ===")

    # Load config with simulator overrides
    config = load_config(args.config, args.market)
    config.sessions = [SessionWindow.parse("00:00-23:59")]
    config.max_buy_notional = 500_000
    config.max_sell_notional = 500_000
    config.full_batch_interval = 2   # minutes (short for testing)
    config.min_dispatch_interval = 5  # seconds
    logger.info(
        "Config loaded: %s (sessions=00:00-23:59, notional caps=$500k, "
        "full batch every 2min, dispatch cooldown 5s)",
        config.name,
    )

    # Universe and lot sizes
    ric_list = load_universe(config.universe_file)
    lot_df = _get_stub_lot_sizes(ric_list)
    lot_sizes = build_lot_size_table(lot_df)
    state_mgr = StateManager(ric_list, lot_sizes, config)

    # Heartbeat
    heartbeat = HeartbeatMonitor(max_staleness=config.max_staleness)

    # GUI
    gui_queue = None
    if args.gui:
        gui_queue = Queue(maxsize=100)
        start_gui_process(gui_queue)

    # Dispatch callback
    def dispatch(df):
        logger.info("DISPATCH to KDB+:\n%s", df.to_string(index=False))

    # Engine
    engine = TradingEngine(
        config=config,
        state_mgr=state_mgr,
        gui_queue=gui_queue,
        dispatch_callback=dispatch,
    )

    # Feed adapters
    loop = asyncio.new_event_loop()

    def push_event(event_type, data):
        engine.event_queue.put_nowait((event_type, data))

    risk_feed = RiskAppetiteFeed(engine_push=push_event)
    price_feed = LivePriceFeed(engine_push=push_event)
    inv_feed = InventoryFeed(engine_push=push_event)
    fills_feed = FillsFeed(engine_push=push_event)
    alpha_feed = AlphaFeed(engine_push=push_event, rics=list(lot_sizes.keys()))

    # Start feeds
    heartbeat.start()
    risk_feed.start(loop)
    price_feed.start(loop)
    inv_feed.start(loop)
    fills_feed.start(loop)
    alpha_feed.start(loop)

    # Simulation control
    running = threading.Event()
    running.set()
    rng = random.Random(args.seed)

    rics = list(lot_sizes.keys())
    sim_threads = [
        threading.Thread(
            target=_sim_risk_appetite, args=(risk_feed, running, rng, rics),
            name="sim-risk", daemon=True,
        ),
        threading.Thread(
            target=_sim_live_price, args=(price_feed, running, rng, rics),
            name="sim-price", daemon=True,
        ),
        threading.Thread(
            target=_sim_inventory, args=(inv_feed, running, rng, rics),
            name="sim-inventory", daemon=True,
        ),
        threading.Thread(
            target=_sim_alpha, args=(alpha_feed, running, rng, rics),
            name="sim-alpha", daemon=True,
        ),
        threading.Thread(
            target=_sim_fills, args=(fills_feed, running, rng, rics),
            name="sim-fills", daemon=True,
        ),
    ]

    # Shutdown handler
    def handle_shutdown(sig, frame):
        logger.info("Received signal %d, shutting down...", sig)
        running.clear()
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(engine.shutdown()))

    signal.signal(signal.SIGINT, handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown)

    # Start sim threads
    for t in sim_threads:
        t.start()
    logger.info("Simulation threads started (seed=%s)", args.seed)

    # Run engine
    try:
        loop.run_until_complete(engine.run())
    finally:
        running.clear()
        heartbeat.stop()
        risk_feed.stop()
        price_feed.stop()
        inv_feed.stop()
        fills_feed.stop()
        alpha_feed.stop()
        for t in sim_threads:
            t.join(timeout=2)
        loop.close()
        logger.info("=== pimm simulator stopped === (log: %s)", log_path)


if __name__ == "__main__":
    main()
