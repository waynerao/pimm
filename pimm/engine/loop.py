# Main asyncio trading loop — shared loop managing multiple markets

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
from pimm.utils.quotetypes import EngineSnapshot, Side, TradeFill
from pimm.utils.time import is_in_session, now_hkt, seconds_until_session_end

logger = logging.getLogger(__name__)

# Internal event types
_EVT_RISK = "risk_appetite"
_EVT_LIVE_PRICE = "live_price"
_EVT_INVENTORY = "inventory"
_EVT_FILLS = "fills"
_EVT_ALPHA = "alpha"
_EVT_SHUTDOWN = "shutdown"
_EVT_COMMAND = "command"


class MarketState:
    # Per-market runtime state (dispatch timing, session, scaling)

    def __init__(self, name, config, state_mgr):
        self.name = name
        self.config = config
        self.state_mgr = state_mgr
        self.session_active = False
        self.quoting_enabled = True
        self.buy_scaling = 1.0
        self.sell_scaling = 1.0
        self.last_full_batch_time = None
        self.last_dispatch_time = None
        self.ric_set = set(state_mgr.df.index)


class TradingEngine:
    # Core trading engine — shared async loop managing multiple markets

    def __init__(self, market_states, gui_queue=None, dispatch_callback=None):
        self._markets = market_states   # dict: market_name -> MarketState
        self._gui_queue = gui_queue
        self._cmd_queue = None          # set by caller for GUI commands
        self._dispatch_callback = dispatch_callback
        self._event_queue = asyncio.Queue()
        self._running = False
        self._recent_fills = []
        self._max_recent_fills = 50
        # Build RIC -> market lookup for routing
        self._ric_to_market = {}
        for mname, ms in self._markets.items():
            for ric in ms.ric_set:
                self._ric_to_market[ric] = mname

    @property
    def event_queue(self):
        return self._event_queue

    def set_cmd_queue(self, q):
        self._cmd_queue = q

    def push_event_threadsafe(self, loop, event_type, data):
        loop.call_soon_threadsafe(self._event_queue.put_nowait, (event_type, data))

    async def run(self):
        self._running = True
        logger.info("Trading engine started (%d markets)", len(self._markets))

        session_task = asyncio.create_task(self._session_monitor())
        cmd_task = asyncio.create_task(self._command_monitor())

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
            cmd_task.cancel()
            for ms in self._markets.values():
                await self._session_end(ms)
            logger.info("Trading engine stopped")

    async def shutdown(self):
        self._running = False
        await self._event_queue.put((_EVT_SHUTDOWN, None))

    async def _handle_event(self, event_type, data):
        if event_type == _EVT_ALPHA:
            # Alpha is per-market, update the specific market
            self._route_alpha(data)
        elif event_type in (_EVT_RISK, _EVT_LIVE_PRICE, _EVT_INVENTORY, _EVT_FILLS):
            # Shared feeds — route rows to correct markets
            self._route_shared_feed(event_type, data)
        else:
            return

        # After state change, try dispatch for affected markets
        affected = self._get_affected_markets(data)
        for mname in affected:
            ms = self._markets.get(mname)
            if ms and ms.session_active and ms.quoting_enabled:
                await self._try_dispatch(ms)

        self._push_gui_snapshot()

    def _route_shared_feed(self, event_type, data):
        # Group rows by market and apply updates
        if "ric" not in data.columns:
            return

        per_market = {}
        for _, row in data.iterrows():
            ric = str(row["ric"])
            mname = self._ric_to_market.get(ric)
            if mname is None:
                continue
            if mname not in per_market:
                per_market[mname] = []
            per_market[mname].append(row)

        for mname, rows in per_market.items():
            ms = self._markets[mname]
            chunk = pd.DataFrame(rows)

            if event_type == _EVT_RISK:
                ms.state_mgr.update_risk_appetite(chunk)
            elif event_type == _EVT_LIVE_PRICE:
                ms.state_mgr.update_live_price(chunk)
            elif event_type == _EVT_INVENTORY:
                ms.state_mgr.update_inventory(chunk)
            elif event_type == _EVT_FILLS:
                self._handle_fills(ms, chunk)

    def _route_alpha(self, data):
        if "ric" not in data.columns:
            return
        per_market = {}
        for _, row in data.iterrows():
            ric = str(row["ric"])
            mname = self._ric_to_market.get(ric)
            if mname is None:
                continue
            if mname not in per_market:
                per_market[mname] = []
            per_market[mname].append(row)

        for mname, rows in per_market.items():
            chunk = pd.DataFrame(rows)
            self._markets[mname].state_mgr.update_alpha(chunk)

    def _get_affected_markets(self, data):
        if "ric" not in data.columns:
            return set()
        markets = set()
        for ric in data["ric"]:
            mname = self._ric_to_market.get(str(ric))
            if mname:
                markets.add(mname)
        return markets

    def _handle_fills(self, ms, df):
        accumulate_fills(ms.state_mgr.df, df)

        for _, row in df.iterrows():
            ric = str(row["ric"])
            side_str = str(row["side"]).lower()
            fill_qty = float(row["fill_qty"])
            fill_price = float(row["fill_price"])

            if ric in ms.state_mgr.df.index:
                cost = fill_price * fill_qty
                if side_str == "buy":
                    ms.state_mgr.df.at[ric, "pnl_buy_qty"] += fill_qty
                    ms.state_mgr.df.at[ric, "pnl_buy_cost"] += cost
                else:
                    ms.state_mgr.df.at[ric, "pnl_sell_qty"] += fill_qty
                    ms.state_mgr.df.at[ric, "pnl_sell_revenue"] += cost

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

        if len(self._recent_fills) > self._max_recent_fills:
            self._recent_fills = self._recent_fills[-self._max_recent_fills:]

    def _is_full_batch_due(self, ms):
        if ms.last_full_batch_time is None:
            return True
        elapsed = now_hkt().timestamp() - ms.last_full_batch_time
        return elapsed >= ms.config.full_batch_interval * 60

    def _is_dispatch_cooldown(self, ms):
        if ms.last_dispatch_time is None:
            return False
        elapsed = now_hkt().timestamp() - ms.last_dispatch_time
        return elapsed < ms.config.min_dispatch_interval

    async def _try_dispatch(self, ms):
        if self._is_dispatch_cooldown(ms):
            return

        if self._is_full_batch_due(ms):
            await self._dispatch_full_batch(ms)
            return

        partial = build_partial_update(
            ms.state_mgr, ms.config,
            ms.buy_scaling, ms.sell_scaling,
        )
        if partial is None:
            return

        exp_buy, exp_sell = compute_notional_impact(
            ms.state_mgr.df, partial
        )
        buy_breach = exp_buy > ms.config.max_buy_notional
        sell_breach = exp_sell > ms.config.max_sell_notional
        if buy_breach or sell_breach:
            logger.info(
                "[%s] Partial would breach notional "
                "(buy=%.0f sell=%.0f), promoting",
                ms.name, exp_buy, exp_sell,
            )
            await self._dispatch_full_batch(ms)
            return

        await self._dispatch_partial(ms, partial)

    async def _dispatch_full_batch(self, ms):
        dispatch, buy_scaling, sell_scaling = build_full_batch(
            ms.state_mgr, ms.config
        )
        ms.buy_scaling = buy_scaling
        ms.sell_scaling = sell_scaling
        now_ts = now_hkt().timestamp()
        ms.last_full_batch_time = now_ts
        ms.last_dispatch_time = now_ts

        if dispatch.empty:
            return

        out_df = dispatch_to_dataframe(dispatch)
        logger.info(
            "[%s] FULL BATCH: %d quotes (scaling buy=%.4f sell=%.4f)",
            ms.name, len(dispatch), buy_scaling, sell_scaling,
        )

        if self._dispatch_callback is not None:
            try:
                self._dispatch_callback(out_df)
            except Exception:
                logger.exception("[%s] Dispatch callback failed", ms.name)

    async def _dispatch_partial(self, ms, partial):
        apply_partial_to_state(ms.state_mgr, partial)
        ms.last_dispatch_time = now_hkt().timestamp()

        out_df = dispatch_to_dataframe(partial)
        logger.info("[%s] PARTIAL UPDATE: %d quotes", ms.name, len(partial))

        if self._dispatch_callback is not None:
            try:
                self._dispatch_callback(out_df)
            except Exception:
                logger.exception("[%s] Dispatch callback failed", ms.name)

    async def _session_monitor(self):
        was_active = {name: False for name in self._markets}
        while self._running:
            await asyncio.sleep(1.0)
            for name, ms in self._markets.items():
                if not ms.quoting_enabled:
                    if ms.session_active:
                        logger.info("[%s] Quoting disabled, ending session", name)
                        await self._session_end(ms)
                        was_active[name] = False
                    continue

                now_active = is_in_session(ms.config)

                if now_active and not was_active[name]:
                    logger.info("[%s] Session started", name)
                    ms.session_active = True
                    ms.state_mgr.reset_pnl()
                    await self._dispatch_full_batch(ms)
                elif not now_active and was_active[name]:
                    logger.info("[%s] Session ended", name)
                    await self._session_end(ms)

                was_active[name] = now_active

    async def _session_end(self, ms):
        ms.session_active = False
        cancel = build_cancel_all(ms.state_mgr)
        if not cancel.empty:
            out_df = dispatch_to_dataframe(cancel)
            logger.info(
                "[%s] Session end: cancelling %d quotes",
                ms.name, len(cancel),
            )
            if self._dispatch_callback is not None:
                try:
                    self._dispatch_callback(out_df)
                except Exception:
                    logger.exception("[%s] Cancel-all dispatch failed", ms.name)

    async def _command_monitor(self):
        # Process GUI commands from cmd_queue
        while self._running:
            await asyncio.sleep(0.1)
            if self._cmd_queue is None:
                await asyncio.sleep(1.0)
                continue
            try:
                while not self._cmd_queue.empty():
                    cmd = self._cmd_queue.get_nowait()
                    await self._process_command(cmd)
            except Exception:
                pass

    async def _process_command(self, cmd):
        action = cmd[0]
        market_name = cmd[1] if len(cmd) > 1 else None
        ms = self._markets.get(market_name)

        if action == "start" and ms:
            logger.info("[%s] GUI command: start quoting", market_name)
            ms.quoting_enabled = True
        elif action == "stop" and ms:
            logger.info("[%s] GUI command: stop quoting", market_name)
            ms.quoting_enabled = False
        elif action == "reload" and ms:
            logger.info("[%s] GUI command: reload config", market_name)
            if len(cmd) > 2:
                new_config = cmd[2]
                ms.config = new_config
                # Update stock limits
                for ric in ms.state_mgr.df.index:
                    ms.state_mgr.df.at[ric, "stock_limit"] = float(
                        new_config.get_stock_limit(ric)
                    )
                logger.info("[%s] Config reloaded", market_name)

    def _push_gui_snapshot(self):
        if self._gui_queue is None:
            return
        try:
            markets = {}
            scaling = {}
            session_status = {}
            session_countdowns = {}
            last_full_batch_times = {}

            for name, ms in self._markets.items():
                markets[name] = ms.state_mgr.copy()
                scaling[name] = (ms.buy_scaling, ms.sell_scaling)
                session_status[name] = ms.session_active
                session_countdowns[name] = seconds_until_session_end(ms.config)
                if ms.last_full_batch_time is not None:
                    last_full_batch_times[name] = ms.last_full_batch_time

            snapshot = EngineSnapshot(
                markets=markets,
                scaling=scaling,
                recent_fills=list(self._recent_fills),
                session_status=session_status,
                session_countdowns=session_countdowns,
                feed_status={},
                timestamp=now_hkt(),
                last_full_batch_times=last_full_batch_times,
            )
            self._gui_queue.put_nowait(snapshot)
        except Exception:
            pass
