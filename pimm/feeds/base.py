# Base adapter: thread management + queue-to-asyncio bridge

import logging
import queue
import threading

logger = logging.getLogger(__name__)


class FeedAdapter:

    def __init__(self, event_type, engine_push, thread=None,
                 service_name=None, table_name=None,
                 recovery_query=None, recovery_params=None,
                 filter_query=None, filter_params=None):
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
        et = self._event_type
        if self._ext_thread is not None:
            try:
                self._ext_thread.start()
                logger.info(f"Feed {et}: external thread started")
            except Exception:
                logger.exception(
                    f"Feed {et}: failed to start external thread"
                )
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name=f"feed-{et}", daemon=True,
        )
        self._poll_thread.start()
        logger.info(f"Feed {et} started")

    def stop(self):
        self._running = False
        et = self._event_type
        if self._ext_thread is not None:
            try:
                if hasattr(self._ext_thread, "stop"):
                    self._ext_thread.stop()
                logger.info(f"Feed {et}: external thread stopped")
            except Exception:
                logger.exception(
                    f"Feed {et}: failed to stop external thread"
                )
        logger.info(f"Feed {et} stopping")

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
        self._push(df)
