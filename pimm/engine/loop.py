# Main asyncio trading loop and session manager

import asyncio
import logging

import pandas as pd

from pimm.engine.dispatcher import (
    apply_partial_to_state,
    build_cancel_all,
    build_full_batch,
    build_partial_update,
    compute_notional_impact,
    dispatch_to_dataframe,
)
from pimm.engine.refill import accumulate_fills
from pimm.types import EngineSnapshot, Side, TradeFill
from pimm.utils.time import is_in_session, now_hkt, seconds_until_session_end

logger = logging.getLogger(__name__)

# Internal event types
_EVT_RISK = "risk_appetite"
_EVT_LIVE_PRICE = "live_price"
_EVT_INVENTORY = "inventory"
_EVT_FILLS = "fills"
_EVT_ALPHA = "alpha"
_EVT_SHUTDOWN = "shutdown"


class TradingEngine:
    # Core trading engine managing the async event loop

    def __init__(self, config, state_mgr, gui_queue=None, dispatch_callback=None):
        self._config = config
        self._state = state_mgr
        self._gui_queue = gui_queue
        self._dispatch_callback = dispatch_callback
        self._event_queue = asyncio.Queue()
        self._running = False
        self._session_active = False
        self._recent_fills = []
        self._max_recent_fills = 50
        self._buy_scaling = 1.0
        self._sell_scaling = 1.0
        self._last_full_batch_time = None
        self._last_dispatch_time = None

    @property
    def event_queue(self):
        return self._event_queue

    def push_event_threadsafe(self, loop, event_type, data):
        # Thread-safe method for feed threads to push events
        loop.call_soon_threadsafe(self._event_queue.put_nowait, (event_type, data))

    async def run(self):
        # Main engine loop
        self._running = True
        logger.info("Trading engine started for market: %s", self._config.name)

        session_task = asyncio.create_task(self._session_monitor())

        try:
            while self._running:
                try:
                    event_type, data = await asyncio.wait_for(
                        self._event_queue.get(), timeout=1.0
                    )
                except TimeoutError:
                    continue

                if event_type == _EVT_SHUTDOWN:
                    logger.info("Shutdown event received")
                    break

                await self._handle_event(event_type, data)
        finally:
            session_task.cancel()
            await self._session_end()
            logger.info("Trading engine stopped")

    async def shutdown(self):
        self._running = False
        await self._event_queue.put((_EVT_SHUTDOWN, None))

    async def _handle_event(self, event_type, data):
        if event_type == _EVT_RISK:
            self._state.update_risk_appetite(data)
        elif event_type == _EVT_LIVE_PRICE:
            self._state.update_live_price(data)
        elif event_type == _EVT_INVENTORY:
            self._state.update_inventory(data)
        elif event_type == _EVT_FILLS:
            self._handle_fills(data)
        elif event_type == _EVT_ALPHA:
            self._state.update_alpha(data)

        # After any state change, try to dispatch if session active
        if self._session_active:
            await self._try_dispatch()

        self._push_gui_snapshot()

    def _handle_fills(self, df):
        # Process trade fill DataFrame
        # Accumulate into universe DataFrame
        accumulate_fills(self._state.df, df)

        # Accumulate PnL accumulators + track recent fills for GUI
        for _, row in df.iterrows():
            ric = str(row["ric"])
            side_str = str(row["side"]).lower()
            fill_qty = float(row["fill_qty"])
            fill_price = float(row["fill_price"])

            if ric in self._state.df.index:
                cost = fill_price * fill_qty
                if side_str == "buy":
                    self._state.df.at[ric, "pnl_buy_qty"] += fill_qty
                    self._state.df.at[ric, "pnl_buy_cost"] += cost
                else:
                    self._state.df.at[ric, "pnl_sell_qty"] += fill_qty
                    self._state.df.at[ric, "pnl_sell_revenue"] += cost

            if "timestamp" in row.index:
                ts = pd.Timestamp(
                    row.get("timestamp", now_hkt())
                ).to_pydatetime()
            else:
                ts = now_hkt()
            self._recent_fills.append(
                TradeFill(
                    ric=ric,
                    side=Side.BUY if side_str == "buy" else Side.SELL,
                    fill_qty=fill_qty,
                    fill_price=fill_price,
                    timestamp=ts,
                )
            )

        # Trim recent fills
        if len(self._recent_fills) > self._max_recent_fills:
            self._recent_fills = self._recent_fills[-self._max_recent_fills:]

    def _is_full_batch_due(self):
        if self._last_full_batch_time is None:
            return True
        elapsed = now_hkt().timestamp() - self._last_full_batch_time
        return elapsed >= self._config.full_batch_interval * 60

    def _is_dispatch_cooldown(self):
        if self._last_dispatch_time is None:
            return False
        elapsed = now_hkt().timestamp() - self._last_dispatch_time
        return elapsed < self._config.min_dispatch_interval

    async def _try_dispatch(self):
        # Dispatch decision flow:
        # 1. Cooldown check
        # 2. Full batch due? -> full batch
        # 3. Compute partial -> nothing? skip
        # 4. Notional check -> breach? promote to full batch
        # 5. Send partial

        if self._is_dispatch_cooldown():
            return

        if self._is_full_batch_due():
            await self._dispatch_full_batch()
            return

        # Try partial update
        partial = build_partial_update(
            self._state, self._config,
            self._buy_scaling, self._sell_scaling,
        )
        if partial is None:
            return

        # Check notional impact
        exp_buy, exp_sell = compute_notional_impact(
            self._state.df, partial
        )
        buy_breach = exp_buy > self._config.max_buy_notional
        sell_breach = exp_sell > self._config.max_sell_notional
        if buy_breach or sell_breach:
            logger.info(
                "Partial would breach notional "
                "(buy=%.0f sell=%.0f), promoting",
                exp_buy, exp_sell,
            )
            await self._dispatch_full_batch()
            return

        # Send partial
        await self._dispatch_partial(partial)

    async def _dispatch_full_batch(self):
        dispatch, buy_scaling, sell_scaling = build_full_batch(
            self._state, self._config
        )
        self._buy_scaling = buy_scaling
        self._sell_scaling = sell_scaling
        now_ts = now_hkt().timestamp()
        self._last_full_batch_time = now_ts
        self._last_dispatch_time = now_ts

        if dispatch.empty:
            return

        out_df = dispatch_to_dataframe(dispatch)
        logger.info(
            "FULL BATCH: %d quotes (scaling buy=%.4f sell=%.4f)",
            len(dispatch), buy_scaling, sell_scaling,
        )

        if self._dispatch_callback is not None:
            try:
                self._dispatch_callback(out_df)
            except Exception:
                logger.exception("Dispatch callback failed")

    async def _dispatch_partial(self, partial):
        apply_partial_to_state(self._state, partial)
        self._last_dispatch_time = now_hkt().timestamp()

        out_df = dispatch_to_dataframe(partial)
        logger.info("PARTIAL UPDATE: %d quotes", len(partial))

        if self._dispatch_callback is not None:
            try:
                self._dispatch_callback(out_df)
            except Exception:
                logger.exception("Dispatch callback failed")

    async def _session_monitor(self):
        # Monitor session windows and manage transitions
        was_active = False
        while self._running:
            await asyncio.sleep(1.0)
            now_active = is_in_session(self._config)

            if now_active and not was_active:
                logger.info("Session started")
                self._session_active = True
                self._state.reset_pnl()
                await self._dispatch_full_batch()
            elif not now_active and was_active:
                logger.info("Session ended")
                await self._session_end()

            was_active = now_active

    async def _session_end(self):
        self._session_active = False
        cancel = build_cancel_all(self._state)
        if not cancel.empty:
            out_df = dispatch_to_dataframe(cancel)
            logger.info("Session end: cancelling all %d quotes", len(cancel))
            if self._dispatch_callback is not None:
                try:
                    self._dispatch_callback(out_df)
                except Exception:
                    logger.exception("Cancel-all dispatch failed")

    def _push_gui_snapshot(self):
        if self._gui_queue is None:
            return
        try:
            snapshot = EngineSnapshot(
                universe=self._state.copy(),
                buy_scaling=self._buy_scaling,
                sell_scaling=self._sell_scaling,
                recent_fills=list(self._recent_fills),
                session_active=self._session_active,
                session_end_countdown=seconds_until_session_end(
                    self._config
                ),
                feed_status={},
                timestamp=now_hkt(),
                last_full_batch_time=self._last_full_batch_time,
            )
            self._gui_queue.put_nowait(snapshot)
        except Exception:
            pass  # GUI queue full, skip
