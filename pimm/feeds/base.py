# Base adapter: thread-to-asyncio bridge for feed subscriptions

import logging
import queue
import threading

logger = logging.getLogger(__name__)


class FeedAdapter:
    # Base class for threaded feed adapters.
    #
    # Production: subclass overrides _subscribe() to call desktool.subscribe()
    # with self._data_queue, then _run() polls the queue automatically.
    #
    # Simulator: call on_update(df) directly from sim threads (bypasses queue).

    def __init__(self, event_type, engine_push):
        self._event_type = event_type
        self._engine_push = engine_push
        self._thread = None
        self._running = False
        self._loop = None
        self._data_queue = queue.Queue()

    def start(self, loop):
        self._running = True
        self._loop = loop
        self._thread = threading.Thread(
            target=self._run,
            name="feed-%s" % self._event_type,
            daemon=True,
        )
        self._thread.start()
        logger.info("Feed %s started", self._event_type)

    def stop(self):
        self._running = False
        logger.info("Feed %s stopping", self._event_type)

    def _run(self):
        try:
            self._subscribe()
        except Exception:
            logger.exception("Feed %s subscribe failed", self._event_type)
            return

        while self._running:
            try:
                df = self._data_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._push(df)

    def _push(self, data):
        if self._running:
            self._loop.call_soon_threadsafe(
                self._engine_push, self._event_type, data
            )

    def on_update(self, df):
        # Direct push for simulator use (bypasses _data_queue)
        self._push(df)

    def _subscribe(self):
        # Override in subclasses to call desktool.subscribe(self._data_queue, ...)
        pass
