# Entry point: wire feeds, engine, and GUI together (multi-market)

import argparse
import asyncio
import logging
import signal
from multiprocessing import Queue

from pimm.config import load_all_markets, load_universe
from pimm.engine.loop import MarketState, TradingEngine
from pimm.engine.state import StateManager
from pimm.feeds.alpha import AlphaFeed
from pimm.feeds.fills import FillsFeed
from pimm.feeds.heartbeat import HeartbeatMonitor
from pimm.feeds.inventory import InventoryFeed
from pimm.feeds.live_price import LivePriceFeed
from pimm.feeds.risk_appetite import RiskAppetiteFeed
from pimm.gui.process import start_gui_process

logger = logging.getLogger("pimm")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _get_stub_lot_sizes(ric_list):
    # Stub lot size table (replace with desktool.get_lot_size())
    stub = {
        "0005.HK": 400, "0700.HK": 100, "9988.HK": 100,
        "1299.HK": 500, "0388.HK": 100,
    }
    return {r: stub[r] for r in ric_list if r in stub}


def main():
    parser = argparse.ArgumentParser(description="pimm — Market Maker Engine")
    parser.add_argument("config", help="Path to config.cfg file")
    args = parser.parse_args()

    _setup_logging()

    # Load all market configs
    all_configs = load_all_markets(args.config)
    logger.info("Loaded %d markets: %s", len(all_configs), list(all_configs.keys()))

    # Build per-market state
    market_states = {}
    all_rics = []
    for mname, config in all_configs.items():
        ric_list = load_universe(config.universe_file)
        lot_sizes = _get_stub_lot_sizes(ric_list)
        state_mgr = StateManager(ric_list, lot_sizes, config)
        market_states[mname] = MarketState(mname, config, state_mgr)
        all_rics.extend(ric_list)
        logger.info("[%s] Universe: %d RICs, %d with lot sizes",
                     mname, len(ric_list), len(lot_sizes))

    # Heartbeat
    max_staleness = max(c.max_staleness for c in all_configs.values())
    heartbeat = HeartbeatMonitor(max_staleness=max_staleness)

    # GUI (always starts)
    gui_queue = Queue(maxsize=100)
    cmd_queue = Queue(maxsize=100)
    start_gui_process(gui_queue, cmd_queue)

    # Dispatch callback
    def dispatch(df):
        logger.info("DISPATCH to KDB+:\n%s", df.to_string(index=False))

    # Engine
    engine = TradingEngine(
        market_states=market_states,
        gui_queue=gui_queue,
        dispatch_callback=dispatch,
    )
    engine.set_cmd_queue(cmd_queue)

    # Feed adapters
    loop = asyncio.new_event_loop()

    def push_event(event_type, data):
        engine.event_queue.put_nowait((event_type, data))

    # Shared feeds
    risk_feed = RiskAppetiteFeed(engine_push=push_event)
    price_feed = LivePriceFeed(engine_push=push_event)
    inv_feed = InventoryFeed(engine_push=push_event)
    fills_feed = FillsFeed(engine_push=push_event)

    # Per-market alpha feeds
    alpha_feeds = {}
    for mname, ms in market_states.items():
        quotable_rics = list(ms.state_mgr.quotable.index)
        af = AlphaFeed(engine_push=push_event, rics=quotable_rics)
        alpha_feeds[mname] = af

    # Start feeds
    heartbeat.start()
    risk_feed.start(loop)
    price_feed.start(loop)
    inv_feed.start(loop)
    fills_feed.start(loop)
    for af in alpha_feeds.values():
        af.start(loop)

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
        price_feed.stop()
        inv_feed.stop()
        fills_feed.stop()
        for af in alpha_feeds.values():
            af.stop()
        loop.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
