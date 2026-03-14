# Entry point: wire feeds, engine, and web server together

import argparse
import asyncio
import logging
import signal

import uvicorn

from pimm.config import (
    load_all_markets,
    load_universe,
    load_web_config,
)
from pimm.engine.loop import MarketState, TradingEngine
from pimm.engine.state import StateManager
from pimm.feeds.alpha import AlphaFeed
from pimm.feeds.fills import FillsFeed
from pimm.feeds.heartbeat import HeartbeatMonitor
from pimm.feeds.inventory import InventoryFeed
from pimm.feeds.live_price import LivePriceFeed
from pimm.feeds.risk_appetite import RiskAppetiteFeed
from pimm.web.server import (
    broadcast_snapshot,
    create_app,
    generate_token,
)

logger = logging.getLogger("pimm")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _get_stub_lot_sizes(ric_list):
    stub = {
        "0005.HK": 400, "0700.HK": 100, "9988.HK": 100,
        "1299.HK": 500, "0388.HK": 100,
    }
    return {r: stub[r] for r in ric_list if r in stub}


class CriticalLogHandler(logging.Handler):
    """Captures CRITICAL+ log messages for web dashboard."""

    def __init__(self, engine):
        super().__init__(level=logging.CRITICAL)
        self._engine = engine

    def emit(self, record):
        msg = self.format(record)
        self._engine.add_console_log(msg)


def main():
    parser = argparse.ArgumentParser(
        description="pimm — Market Maker Engine"
    )
    parser.add_argument(
        "config", help="Path to config.cfg file"
    )
    args = parser.parse_args()

    _setup_logging()

    all_configs = load_all_markets(args.config)
    web_config = load_web_config(args.config)
    logger.info(
        f"Loaded {len(all_configs)} markets: "
        f"{list(all_configs.keys())}"
    )

    # Build per-market state
    market_states = {}
    for mname, config in all_configs.items():
        ric_list = load_universe(config.universe_file)
        lot_sizes = _get_stub_lot_sizes(ric_list)
        state_mgr = StateManager(ric_list, lot_sizes, config)
        market_states[mname] = MarketState(
            mname, config, state_mgr
        )
        logger.info(
            f"[{mname}] Universe: {len(ric_list)} RICs, "
            f"{len(lot_sizes)} with lot sizes"
        )

    max_staleness = max(
        c.max_staleness for c in all_configs.values()
    )
    heartbeat = HeartbeatMonitor(max_staleness=max_staleness)

    def dispatch(df):
        logger.info(
            f"DISPATCH to KDB+:\n{df.to_string(index=False)}"
        )

    loop = asyncio.new_event_loop()

    def push_event(event_type, data):
        loop.call_soon_threadsafe(
            lambda: engine.event_queue.put_nowait(
                (event_type, data)
            )
        )

    risk_feed = RiskAppetiteFeed(engine_push=push_event)
    price_feed = LivePriceFeed(engine_push=push_event)
    fills_feed = FillsFeed(engine_push=push_event)
    inv_feed = InventoryFeed(engine_push=push_event)
    alpha_feed = AlphaFeed(engine_push=push_event)

    for mname, ms in market_states.items():
        inv_feed.register_market(mname)
        if ms.config.alpha_enabled:
            quotable_rics = list(ms.state_mgr.quotable.index)
            alpha_feed.register_market(
                mname, rics=quotable_rics
            )

    # Web server
    token = generate_token()
    engine_ref = {}
    app = create_app(engine_ref, token)

    async def snapshot_cb(snap):
        await broadcast_snapshot(app, snap)

    engine = TradingEngine(
        market_states=market_states,
        dispatch_callback=dispatch,
        snapshot_callback=snapshot_cb,
        inventory_feed=inv_feed,
        alpha_feed=alpha_feed, config_path=args.config,
    )

    engine_ref["cmd_callback"] = lambda action, market: (
        engine.process_web_command(action, market)
    )

    crit_handler = CriticalLogHandler(engine)
    crit_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s", "%H:%M:%S"
    ))
    logging.getLogger().addHandler(crit_handler)

    # Start feeds
    heartbeat.start()
    risk_feed.start(loop)
    price_feed.start(loop)
    fills_feed.start(loop)
    inv_feed.set_loop(loop)
    alpha_feed.set_loop(loop)

    url = f"http://localhost:{web_config.port}/?token={token}"
    logger.info(f"Web dashboard: {url}")
    print(f"\n  Dashboard URL: {url}\n")

    def handle_shutdown(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(engine.shutdown())
        )

    signal.signal(signal.SIGINT, handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown)

    async def run_all():
        uvi_config = uvicorn.Config(
            app, host="0.0.0.0", port=web_config.port,
            log_level="warning", loop="none",
        )
        server = uvicorn.Server(uvi_config)
        server.install_signal_handlers = lambda: None

        engine_task = asyncio.create_task(engine.run())
        server_task = asyncio.create_task(server.serve())

        done, pending = await asyncio.wait(
            [engine_task, server_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
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
        for mname in market_states:
            inv_feed.stop_market(mname)
            alpha_feed.stop_market(mname)
        loop.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
