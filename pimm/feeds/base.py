# Base adapter: thread management + queue-to-asyncio bridge for feed subscriptions

import logging
import queue
import threading

logger = logging.getLogger(__name__)


class FeedAdapter:
    # Base class for feed adapters.
    #
    # Production (desktool feeds): receives a thread object from desktool.
    # Engine controls start/stop. Desktool pushes DataFrames into data_queue;
    # a polling loop forwards them to the engine's asyncio queue.
    #
    # Alpha feed: no thread object. External project pushes to data_queue.
    # Polling loop forwards to engine.
    #
    # Simulator: call on_update(df) directly (bypasses queue).

    def __init__(
        self,
        event_type,
        engine_push,
        thread=None,
        service_name=None,
        table_name=None,
        recovery_query=None,
        recovery_params=None,
        filter_query=None,
        filter_params=None,
    ):
        self._event_type = event_type
        self._engine_push = engine_push
        self._ext_thread = thread
        self._service_name = service_name
        self._table_name = table_name
        self._recovery_query = recovery_query
        self._recovery_params = recovery_params
        self._filter_query = filter_query
        self._filter_params = filter_params
        self._poll_thread = None
        self._running = False
        self._loop = None
        self._data_queue = queue.Queue()

    def start(self, loop):
        self._running = True
        self._loop = loop

        # Start the desktool thread if provided
        if self._ext_thread is not None:
            try:
                self._ext_thread.start()
                logger.info("Feed %s: external thread started", self._event_type)
            except Exception:
                logger.exception(
                    "Feed %s: failed to start external thread",
                    self._event_type,
                )

        # Start queue polling thread
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="feed-%s" % self._event_type,
            daemon=True,
        )
        self._poll_thread.start()
        logger.info("Feed %s started", self._event_type)

    def stop(self):
        self._running = False
        if self._ext_thread is not None:
            try:
                if hasattr(self._ext_thread, "stop"):
                    self._ext_thread.stop()
                logger.info("Feed %s: external thread stopped", self._event_type)
            except Exception:
                logger.exception(
                    "Feed %s: failed to stop external thread",
                    self._event_type,
                )
        logger.info("Feed %s stopping", self._event_type)

    def _poll_loop(self):
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
