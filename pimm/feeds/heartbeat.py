# Feed staleness monitor and alerting

import logging
import platform
import threading
import time

from pimm.utils.time import now_hkt

logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    # Monitors feed staleness and triggers winsound.Beep alerts

    def __init__(self, max_staleness):
        self._max_staleness = max_staleness
        self._last_update = {}
        self._thread = None
        self._running = False
        self._stale_feeds = set()

    @property
    def stale_feeds(self):
        return self._stale_feeds

    def record_update(self, feed_name):
        self._last_update[feed_name] = now_hkt()
        self._stale_feeds.discard(feed_name)

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="heartbeat-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Heartbeat monitor started (max_staleness=%ds)",
            self._max_staleness,
        )

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        while self._running:
            time.sleep(1.0)
            now = now_hkt()
            for feed_name, last in self._last_update.items():
                elapsed = (now - last).total_seconds()
                if elapsed > self._max_staleness and feed_name not in self._stale_feeds:
                    self._stale_feeds.add(feed_name)
                    logger.warning(
                        "Feed '%s' is STALE (%.0fs > %ds)",
                        feed_name, elapsed, self._max_staleness,
                    )
                    self._beep()

    def _beep(self):
        if platform.system() == "Windows":
            try:
                import winsound
                winsound.Beep(1000, 500)
            except Exception:
                logger.warning("winsound.Beep failed")
        else:
            logger.warning("ALERT: Feed staleness detected (no winsound)")
