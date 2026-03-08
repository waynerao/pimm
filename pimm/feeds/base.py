# Base adapter: thread-to-asyncio bridge for feed subscriptions

import logging
import threading

logger = logging.getLogger(__name__)


class FeedAdapter:
    # Base class for threaded feed adapters.
    # Subclasses implement _subscribe() and call _push(data) to send updates.

    def __init__(self, event_type, engine_push):
        self._event_type = event_type
        self._engine_push = engine_push
        self._thread = None
        self._running = False
        self._loop = None

    def start(self, loop):
        # Start the feed subscription in a background thread
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
            logger.exception("Feed %s failed", self._event_type)

    def _push(self, data):
        # Push data to the engine event queue (thread-safe)
        if self._running:
            self._loop.call_soon_threadsafe(
                self._engine_push, self._event_type, data
            )

    def _subscribe(self):
        # Override in subclasses
        raise NotImplementedError
