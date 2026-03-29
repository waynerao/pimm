# Entry point: wire feeds, engine, and web server together

import argparse
import asyncio
import logging
import os
import signal
from datetime import datetime

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

logger = logging.getLogger("pimm")


def _setup_logging():
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%H:%M:%S")
    os.makedirs("logs", exist_ok=True)
    fh = logging.FileHandler(f"logs/pimm_{datetime.now():%Y%m%d}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(fh)


def _get_trading_day_type(market):
    """Stub: always returns 1 until desktool is wired."""
    return 1


class WebLogHandler(logging.Handler):
    """Captures log messages for web dashboard."""

    def __init__(self, engine):
        super().__init__(level=logging.INFO)
        self._engine = engine

    def emit(self, record):
        msg = self.format(record)
        self._engine.add_console_log(record.levelname, msg)


def main():
    parser = argparse.ArgumentParser(description="pimm — Market Maker Engine")
    parser.add_argument("config", help="Path to config.toml file")
    args = parser.parse_args()

    _setup_logging()

    pimm_config = load_pimm_config(args.config)
    all_configs = load_all_markets(args.config)
    logger.info(f"Loaded {len(all_configs)} markets: {list(all_configs.keys())}")

    # Build per-market state
    market_states = {}
    for mkt, config in all_configs.items():
        ric_list = load_universe(config.universe_file)
        state_mgr = StateManager(ric_list, config)
        day_type = _get_trading_day_type(mkt)
        market_states[mkt] = MarketState(mkt, config, state_mgr, day_type=day_type)
        logger.info(f"[{mkt}] day_type={day_type}")

    heartbeat = HeartbeatMonitor(max_staleness_s=pimm_config.max_staleness_s)

    def dispatch(df):
        logger.info(f"DISPATCH to KDB+:\n{df.to_string(index=False)}")

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

    url = f"http://{get_host_ip()}:{pimm_config.web_port}/?token={token}"
    logger.info(f"Web dashboard: {url}")
    print(f"\n  Dashboard URL: {url}\n")

    def handle_shutdown(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(engine.shutdown()))

    signal.signal(signal.SIGINT, handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown)

    async def run_all():
        uvi_config = uvicorn.Config(app, host="0.0.0.0", port=pimm_config.web_port, log_level="warning", loop="none")
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
        loop.run_until_complete(engine.shutdown())
    finally:
        heartbeat.stop()
        risk_feed.stop()
        price_feed.stop()
        fills_feed.stop()
        for inv in inventory_feeds.values():
            inv.stop()
        for alpha in alpha_feeds.values():
            alpha.stop()
        loop.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
