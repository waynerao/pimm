# Alpha signal feed — one instance managing per-country queues

import logging
import queue
import threading

import pandas as pd

logger = logging.getLogger(__name__)


class AlphaFeed:

    def __init__(self, engine_push):
        self._engine_push = engine_push
        self._loop = None
        self._markets = {}

    def register_market(self, market, rics=None):
        self._markets[market] = {
            "rics": rics or [], "data_queue": queue.Queue(),
            "poll_thread": None, "running": False,
        }

    def start_market(self, market):
        entry = self._markets.get(market)
        if entry is None or entry["running"]:
            return
        entry["running"] = True
        entry["poll_thread"] = threading.Thread(
            target=self._poll_loop, args=(market, entry),
            name=f"feed-alpha-{market}", daemon=True,
        )
        entry["poll_thread"].start()
        if entry["rics"]:
            stub = pd.DataFrame({
                "ric": entry["rics"],
                "alpha": [0.0] * len(entry["rics"]),
            })
            if self._loop is not None:
                self._loop.call_soon_threadsafe(
                    self._engine_push, "alpha", stub
                )
        logger.info(
            f"AlphaFeed [{market}]: started "
            f"({len(entry['rics'])} rics)"
        )

    def stop_market(self, market):
        entry = self._markets.get(market)
        if entry is None or not entry["running"]:
            return
        entry["running"] = False
        logger.info(f"AlphaFeed [{market}]: stopped")

    def set_loop(self, loop):
        self._loop = loop

    def on_update(self, df, market=None):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                self._engine_push, "alpha", df
            )

    def _poll_loop(self, market, entry):
        while entry["running"]:
            try:
                df = entry["data_queue"].get(timeout=1.0)
            except queue.Empty:
                continue
            if self._loop is not None and entry["running"]:
                self._loop.call_soon_threadsafe(
                    self._engine_push, "alpha", df
                )
