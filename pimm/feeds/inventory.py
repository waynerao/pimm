# Inventory feed — one instance managing per-country subscriptions

import logging
import queue
import threading

logger = logging.getLogger(__name__)


class InventoryFeed:

    def __init__(self, engine_push):
        self._engine_push = engine_push
        self._loop = None
        self._markets = {}

    def register_market(self, market, thread=None, **kwargs):
        self._markets[market] = {
            "thread": thread, "data_queue": queue.Queue(),
            "poll_thread": None, "running": False,
            "kwargs": kwargs,
        }

    def start_market(self, market):
        entry = self._markets.get(market)
        if entry is None or entry["running"]:
            return
        entry["running"] = True
        if entry["thread"] is not None:
            try:
                entry["thread"].start()
                logger.info(
                    f"InventoryFeed [{market}]: "
                    f"external thread started"
                )
            except Exception:
                logger.exception(
                    f"InventoryFeed [{market}]: "
                    f"failed to start thread"
                )
        entry["poll_thread"] = threading.Thread(
            target=self._poll_loop, args=(market, entry),
            name=f"feed-inventory-{market}", daemon=True,
        )
        entry["poll_thread"].start()
        logger.info(f"InventoryFeed [{market}]: started")

    def stop_market(self, market):
        entry = self._markets.get(market)
        if entry is None or not entry["running"]:
            return
        entry["running"] = False
        if entry["thread"] is not None:
            try:
                if hasattr(entry["thread"], "stop"):
                    entry["thread"].stop()
                logger.info(
                    f"InventoryFeed [{market}]: "
                    f"external thread stopped"
                )
            except Exception:
                logger.exception(
                    f"InventoryFeed [{market}]: "
                    f"failed to stop thread"
                )
        logger.info(f"InventoryFeed [{market}]: stopped")

    def set_loop(self, loop):
        self._loop = loop

    def on_update(self, df, market=None):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                self._engine_push, "inventory", df
            )

    def _poll_loop(self, market, entry):
        while entry["running"]:
            try:
                df = entry["data_queue"].get(timeout=1.0)
            except queue.Empty:
                continue
            if self._loop is not None and entry["running"]:
                self._loop.call_soon_threadsafe(
                    self._engine_push, "inventory", df
                )
