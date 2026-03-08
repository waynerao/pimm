# Entry point: wire feeds, engine, and GUI together

import argparse
import asyncio
import logging
import signal
from multiprocessing import Queue

import pandas as pd

from configs.config import load_config, load_universe
from pimm.engine.loop import TradingEngine
from pimm.engine.state import StateManager
from pimm.feeds.alpha import AlphaFeed
from pimm.feeds.fills import FillsFeed
from pimm.feeds.heartbeat import HeartbeatMonitor
from pimm.feeds.inventory import InventoryFeed
from pimm.feeds.risk_appetite import RiskAppetiteFeed
from pimm.gui.process import start_gui_process
from pimm.utils.lots import build_lot_size_table

logger = logging.getLogger("pimm")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _get_stub_lot_sizes(ric_list):
    # Stub lot size table (replace with desktool call)
    stub = {
        "0005.HK": 400, "0700.HK": 100, "9988.HK": 100,
        "1299.HK": 500, "0388.HK": 100,
    }
    rows = [{"ric": r, "lot_size": stub.get(r, 100)} for r in ric_list]
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="pimm — Market Maker Engine")
    parser.add_argument("config", help="Path to market TOML config file")
    parser.add_argument("--market", default="HK", help="Market section name in TOML")
    parser.add_argument("--no-gui", action="store_true", help="Run without GUI")
    args = parser.parse_args()

    _setup_logging()

    # Load config and universe
    config = load_config(args.config, args.market)
    logger.info("Loaded config for market: %s", config.name)

    ric_list = load_universe(config.universe_file)
    logger.info("Universe loaded: %d RICs", len(ric_list))

    # Lot sizes (stub)
    lot_df = _get_stub_lot_sizes(ric_list)
    lot_sizes = build_lot_size_table(lot_df)
    logger.info("Lot sizes loaded: %d RICs", len(lot_sizes))

    # State manager
    state_mgr = StateManager(ric_list, lot_sizes, config)

    # Heartbeat
    heartbeat = HeartbeatMonitor(max_staleness=config.max_staleness)

    # GUI
    gui_queue = None
    if not args.no_gui:
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
    inv_feed = InventoryFeed(engine_push=push_event)
    fills_feed = FillsFeed(engine_push=push_event)
    alpha_feed = AlphaFeed(engine_push=push_event, rics=list(lot_sizes.keys()))

    # Start feeds
    heartbeat.start()
    risk_feed.start(loop)
    inv_feed.start(loop)
    fills_feed.start(loop)
    alpha_feed.start(loop)

    # Shutdown handler
    def handle_shutdown(sig, frame):
        logger.info("Received signal %d, shutting down...", sig)
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(engine.shutdown()))

    signal.signal(signal.SIGINT, handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown)

    # Run
    try:
        loop.run_until_complete(engine.run())
    finally:
        heartbeat.stop()
        risk_feed.stop()
        inv_feed.stop()
        fills_feed.stop()
        alpha_feed.stop()
        loop.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
