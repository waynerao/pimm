# Tests for feed adapter base class and concrete feed adapters

import asyncio
import threading
import time

import pandas as pd

from pimm.feeds.alpha import AlphaFeed
from pimm.feeds.base import FeedAdapter
from pimm.feeds.fills import FillsFeed
from pimm.feeds.inventory import InventoryFeed
from pimm.feeds.live_price import LivePriceFeed
from pimm.feeds.risk_appetite import RiskAppetiteFeed


def _capture(*containers):
    # Returns a push function that appends (event_type, data) to containers
    def push(event_type, data):
        for c in containers:
            c.append((event_type, data))
    return push


def _capture_data(container):
    # Returns a push function that appends just the data
    return lambda et, d: container.append(d)


def _make_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


def _stop_loop(loop, thread):
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


class TestFeedAdapterBase:
    def test_on_update_pushes_to_engine(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = FeedAdapter(event_type="test", engine_push=_capture(received))
            feed.start(loop)
            df = pd.DataFrame({"ric": ["A"], "value": [1.0]})
            feed.on_update(df)
            time.sleep(0.2)
            assert len(received) == 1
            assert received[0][0] == "test"
            assert received[0][1].equals(df)
        finally:
            feed.stop()
            _stop_loop(loop, t)

    def test_data_queue_polling(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = FeedAdapter(event_type="test", engine_push=_capture(received))
            feed.start(loop)
            df = pd.DataFrame({"ric": ["B"], "value": [2.0]})
            feed._data_queue.put(df)
            time.sleep(1.5)
            assert len(received) == 1
            assert received[0][1].equals(df)
        finally:
            feed.stop()
            _stop_loop(loop, t)

    def test_multiple_pushes(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = FeedAdapter(event_type="test", engine_push=_capture(received))
            feed.start(loop)
            for i in range(5):
                feed.on_update(pd.DataFrame({"v": [i]}))
            time.sleep(0.3)
            assert len(received) == 5
        finally:
            feed.stop()
            _stop_loop(loop, t)

    def test_stop_prevents_further_pushes(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = FeedAdapter(event_type="test", engine_push=_capture(received))
            feed.start(loop)
            feed.stop()
            time.sleep(0.1)
            feed.on_update(pd.DataFrame({"v": [1]}))
            time.sleep(0.2)
            assert len(received) == 0
        finally:
            _stop_loop(loop, t)

    def test_subscribe_exception_does_not_crash(self):
        class BadFeed(FeedAdapter):
            def _subscribe(self):
                raise RuntimeError("subscribe failed")

        loop, t = _make_loop()
        try:
            feed = BadFeed(event_type="bad", engine_push=lambda et, d: None)
            feed.start(loop)
            time.sleep(0.3)
            # Thread should have exited without crashing the process
            assert not feed._thread.is_alive()
        finally:
            feed.stop()
            _stop_loop(loop, t)

    def test_queue_and_on_update_interleave(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = FeedAdapter(event_type="test", engine_push=_capture_data(received))
            feed.start(loop)
            feed.on_update(pd.DataFrame({"src": ["on_update"]}))
            feed._data_queue.put(pd.DataFrame({"src": ["queue"]}))
            time.sleep(1.5)
            assert len(received) == 2
            sources = [df["src"].iloc[0] for df in received]
            assert "on_update" in sources
            assert "queue" in sources
        finally:
            feed.stop()
            _stop_loop(loop, t)


class TestRiskAppetiteFeed:
    def test_event_type(self):
        feed = RiskAppetiteFeed(engine_push=lambda et, d: None)
        assert feed._event_type == "risk_appetite"

    def test_pushes_risk_data(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = RiskAppetiteFeed(engine_push=_capture(received))
            feed.start(loop)
            df = pd.DataFrame({
                "ric": ["0005.HK"],
                "buy_state": ["best_bid"],
                "buy_qty": [1000.0],
                "sell_state": ["best_offer"],
                "sell_qty": [2000.0],
                "fx_rate": [0.128],
            })
            feed.on_update(df)
            time.sleep(0.2)
            assert len(received) == 1
            assert received[0][0] == "risk_appetite"
        finally:
            feed.stop()
            _stop_loop(loop, t)


class TestLivePriceFeed:
    def test_event_type(self):
        feed = LivePriceFeed(engine_push=lambda et, d: None)
        assert feed._event_type == "live_price"

    def test_pushes_price_data(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = LivePriceFeed(engine_push=_capture(received))
            feed.start(loop)
            df = pd.DataFrame({"ric": ["0005.HK"], "last_price": [60.0]})
            feed.on_update(df)
            time.sleep(0.2)
            assert len(received) == 1
            assert received[0][0] == "live_price"
        finally:
            feed.stop()
            _stop_loop(loop, t)


class TestInventoryFeed:
    def test_event_type(self):
        feed = InventoryFeed(engine_push=lambda et, d: None)
        assert feed._event_type == "inventory"

    def test_pushes_inventory_data(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = InventoryFeed(engine_push=_capture(received))
            feed.start(loop)
            df = pd.DataFrame({"ric": ["0005.HK"], "inventory": [5000.0]})
            feed.on_update(df)
            time.sleep(0.2)
            assert len(received) == 1
            assert received[0][0] == "inventory"
        finally:
            feed.stop()
            _stop_loop(loop, t)


class TestFillsFeed:
    def test_event_type(self):
        feed = FillsFeed(engine_push=lambda et, d: None)
        assert feed._event_type == "fills"

    def test_pushes_fill_data(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = FillsFeed(engine_push=_capture(received))
            feed.start(loop)
            df = pd.DataFrame({
                "ric": ["0005.HK"],
                "side": ["buy"],
                "fill_qty": [100.0],
                "fill_price": [60.0],
                "timestamp": pd.Timestamp.now(),
            })
            feed.on_update(df)
            time.sleep(0.2)
            assert len(received) == 1
            assert received[0][0] == "fills"
        finally:
            feed.stop()
            _stop_loop(loop, t)


class TestAlphaFeed:
    def test_event_type(self):
        feed = AlphaFeed(engine_push=lambda et, d: None)
        assert feed._event_type == "alpha"

    def test_startup_pushes_zero_alpha(self):
        received = []
        loop, t = _make_loop()
        try:
            rics = ["0005.HK", "0700.HK"]
            feed = AlphaFeed(
                engine_push=_capture(received),
                rics=rics,
            )
            feed.start(loop)
            time.sleep(0.3)
            assert len(received) >= 1
            df = received[0][1]
            assert list(df["ric"]) == rics
            assert all(df["alpha"] == 0.0)
        finally:
            feed.stop()
            _stop_loop(loop, t)

    def test_no_rics_no_startup_push(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = AlphaFeed(engine_push=_capture(received))
            feed.start(loop)
            time.sleep(0.3)
            assert len(received) == 0
        finally:
            feed.stop()
            _stop_loop(loop, t)

    def test_pushes_alpha_data(self):
        received = []
        loop, t = _make_loop()
        try:
            feed = AlphaFeed(engine_push=_capture(received))
            feed.start(loop)
            df = pd.DataFrame({"ric": ["0005.HK"], "alpha": [0.25]})
            feed.on_update(df)
            time.sleep(0.2)
            assert len(received) == 1
            assert received[0][0] == "alpha"
        finally:
            feed.stop()
            _stop_loop(loop, t)
