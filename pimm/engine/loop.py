# Main asyncio trading loop — shared loop managing multiple markets

import asyncio
import logging

import pandas as pd

from pimm.config import load_market_config, load_pimm_config
from pimm.engine.dispatcher import (apply_partial_to_state, build_cancel_all, build_full_batch,
                                     build_partial_update, compute_notional_impact, dispatch_to_dataframe)
from pimm.engine.refill import accumulate_fills
from pimm.utils.quotetypes import EngineSnapshot, Side, TradeFill
from pimm.utils.time import (current_session_window, is_in_session, next_session_window, now_hkt,
                              seconds_until_session_end)

logger = logging.getLogger(__name__)

_EVT_RISK = "risk_appetite"
_EVT_LIVE_PRICE = "live_price"
_EVT_INVENTORY = "inventory"
_EVT_FILLS = "fills"
_EVT_ALPHA = "alpha"
_EVT_SHUTDOWN = "shutdown"
_EVT_COMMAND = "command"


def _get_stub_delta_beta_info(markets):
    """Stub: replace with desktool.get_delta_beta_info() when wired.

    Returns a multi-line string with per-market delta/beta summary.
    Real implementation should call desktool to get portfolio-level greeks.
    """
    lines = []
    for name, ms in sorted(markets.items()):
        if not ms.session_active:
            lines.append(f"{name:4s}  INACTIVE")
            continue
        df = ms.state_mgr.df
        inv = df["inventory"].sum() if "inventory" in df.columns else 0
        lines.append(f"{name:4s}  delta={inv:>10,.0f}  beta=--")
    return "\n".join(lines)


class MarketState:
    def __init__(self, name, config, state_mgr, day_type=1):
        self.name, self.config = name, config
        self.state_mgr = state_mgr
        self.session_active = False
        self.quoting_enabled = True
        self.buy_scaling = self.sell_scaling = 1.0
        self.last_full_batch_time = None
        self.last_dispatch_time = None
        self.ric_set = set(state_mgr.df.index)
        self.day_type = day_type
        self.override_window = None


class TradingEngine:
    def __init__(self, market_states, config_path, dispatch_callback=None, snapshot_callback=None,
                 inventory_feeds=None, alpha_feeds=None):
        self._markets = market_states
        self._config_path = config_path
        self._pimm_config = load_pimm_config(config_path)
        self._dispatch_callback = dispatch_callback
        self._snapshot_callback = snapshot_callback
        self._inventory_feeds = inventory_feeds or {}
        self._alpha_feeds = alpha_feeds or {}
        self._event_queue = asyncio.Queue()
        self._running = False
        self._recent_fills = []
        self._max_recent_fills = 50
        self._delta_beta_info = ""
        self._console_log = []
        self._max_console_log = 2000

    @property
    def event_queue(self):
        return self._event_queue

    def push_event_threadsafe(self, loop, event_type, data):
        loop.call_soon_threadsafe(self._event_queue.put_nowait, (event_type, data))

    def add_console_log(self, level, msg):
        self._console_log.append({"level": level, "msg": msg})
        if len(self._console_log) > self._max_console_log:
            self._console_log = self._console_log[-self._max_console_log :]

    def set_delta_beta_info(self, info):
        self._delta_beta_info = info

    def process_web_command(self, action, market_name):
        self._event_queue.put_nowait((_EVT_COMMAND, (action, market_name)))

    async def run(self):
        self._running = True
        logger.info(f"Trading engine started ({len(self._markets)} markets)")
        session_task = asyncio.create_task(self._session_monitor())
        snapshot_task = asyncio.create_task(self._snapshot_loop())
        delta_beta_task = asyncio.create_task(self._delta_beta_loop())
        try:
            while self._running:
                try:
                    event_type, data = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if event_type == _EVT_SHUTDOWN:
                    logger.info("Shutdown event received")
                    break
                if event_type == _EVT_COMMAND:
                    await self._process_command(data)
                    continue
                await self._handle_event(event_type, data)
        finally:
            session_task.cancel()
            snapshot_task.cancel()
            delta_beta_task.cancel()
            for ms in self._markets.values():
                await self._session_end(ms)
            logger.info("Trading engine stopped")

    async def shutdown(self):
        self._running = False
        await self._event_queue.put((_EVT_SHUTDOWN, None))

    async def _handle_event(self, event_type, data):
        update_method = {
            _EVT_RISK: "update_risk_appetite",
            _EVT_LIVE_PRICE: "update_live_price",
            _EVT_INVENTORY: "update_inventory",
            _EVT_ALPHA: "update_alpha",
        }.get(event_type)
        if update_method is None:
            if event_type == _EVT_FILLS:
                for ms in self._markets.values():
                    self._handle_fills(ms, data)
                    if ms.session_active and ms.quoting_enabled:
                        await self._try_dispatch(ms)
            return
        for ms in self._markets.values():
            method = getattr(ms.state_mgr, update_method)
            matched = method(data)
            if matched and ms.session_active and ms.quoting_enabled:
                await self._try_dispatch(ms)

    def _handle_fills(self, ms, df):
        if "ric" not in df.columns:
            return
        incoming = df.set_index("ric") if "ric" in df.columns else df
        common = ms.state_mgr.df.index.intersection(incoming.index)
        if common.empty:
            return
        chunk = df[df["ric"].isin(common)]
        accumulate_fills(ms.state_mgr.df, chunk)
        for _, row in chunk.iterrows():
            ric = str(row["ric"])
            side_str = str(row["side"]).lower()
            fill_qty = float(row["fill_qty"])
            fill_price = float(row["fill_price"])
            cost = fill_price * fill_qty
            if side_str == "buy":
                ms.state_mgr.df.at[ric, "pnl_buy_qty"] += fill_qty
                ms.state_mgr.df.at[ric, "pnl_buy_cost"] += cost
            else:
                ms.state_mgr.df.at[ric, "pnl_sell_qty"] += fill_qty
                ms.state_mgr.df.at[ric, "pnl_sell_revenue"] += cost
            ts = (
                pd.Timestamp(row.get("timestamp", now_hkt())).to_pydatetime() if "timestamp" in row.index else now_hkt()
            )
            side = Side.BUY if side_str == "buy" else Side.SELL
            self._recent_fills.append(
                TradeFill(ric=ric, side=side, fill_qty=fill_qty, fill_price=fill_price, timestamp=ts)
            )
        if len(self._recent_fills) > self._max_recent_fills:
            self._recent_fills = self._recent_fills[-self._max_recent_fills :]

    def _is_full_batch_due(self, ms):
        if ms.last_full_batch_time is None:
            return True
        elapsed = now_hkt().timestamp() - ms.last_full_batch_time
        return elapsed >= self._pimm_config.full_batch_interval_m * 60

    def _is_dispatch_cooldown(self, ms):
        if ms.last_dispatch_time is None:
            return False
        elapsed = now_hkt().timestamp() - ms.last_dispatch_time
        return elapsed < self._pimm_config.min_dispatch_interval_s

    async def _try_dispatch(self, ms):
        if self._is_dispatch_cooldown(ms):
            return
        if self._is_full_batch_due(ms):
            await self._dispatch_full_batch(ms)
            return
        partial = build_partial_update(ms.state_mgr, ms.config, ms.buy_scaling, ms.sell_scaling)
        if partial is None:
            return
        exp_buy, exp_sell = compute_notional_impact(ms.state_mgr.df, partial)
        if exp_buy > ms.config.max_buy_notional or exp_sell > ms.config.max_sell_notional:
            logger.info(f"[{ms.name}] Partial would breach notional (buy={exp_buy:.0f} sell={exp_sell:.0f}), promoting")
            await self._dispatch_full_batch(ms)
            return
        await self._dispatch_partial(ms, partial)

    async def _dispatch_full_batch(self, ms):
        dispatch, buy_s, sell_s = build_full_batch(ms.state_mgr, ms.config)
        ms.buy_scaling, ms.sell_scaling = buy_s, sell_s
        now_ts = now_hkt().timestamp()
        ms.last_full_batch_time = now_ts
        ms.last_dispatch_time = now_ts
        if dispatch.empty:
            return
        out_df = dispatch_to_dataframe(dispatch)
        logger.info(f"[{ms.name}] FULL BATCH: {len(dispatch)} quotes (scaling buy={buy_s:.4f} sell={sell_s:.4f})")
        if self._dispatch_callback is not None:
            try:
                self._dispatch_callback(out_df)
            except Exception:
                logger.exception(f"[{ms.name}] Dispatch callback failed")

    async def _dispatch_partial(self, ms, partial):
        apply_partial_to_state(ms.state_mgr, partial)
        ms.last_dispatch_time = now_hkt().timestamp()
        out_df = dispatch_to_dataframe(partial)
        logger.info(f"[{ms.name}] PARTIAL UPDATE: {len(partial)} quotes")
        if self._dispatch_callback is not None:
            try:
                self._dispatch_callback(out_df)
            except Exception:
                logger.exception(f"[{ms.name}] Dispatch callback failed")

    def _should_auto_activate(self, ms, window_index):
        """Check if a session window should auto-activate."""
        if ms.day_type == 1:
            return True
        if ms.day_type == 0.5:
            return window_index == 0
        return False

    async def _session_monitor(self):
        was_active = {name: False for name in self._markets}
        while self._running:
            await asyncio.sleep(1.0)
            for name, ms in self._markets.items():
                if not ms.quoting_enabled:
                    if ms.session_active:
                        logger.info(f"[{name}] Quoting disabled, ending session")
                        await self._session_end(ms)
                        self._stop_market_feeds(name)
                        was_active[name] = False
                    continue

                now = now_hkt()
                now_in, win_idx = is_in_session(ms.config, now, return_index=True)

                # Check override window
                if ms.override_window is not None:
                    ow = ms.override_window
                    from pimm.utils.time import session_window_to_times

                    ow_start, ow_end = session_window_to_times(ow)
                    in_override = ow_start <= now.time() < ow_end
                    if in_override and not was_active[name]:
                        logger.info(f"[{name}] Override session started")
                        ms.session_active = True
                        ms.state_mgr.reset_pnl()
                        self._start_market_feeds(name)
                        await self._dispatch_full_batch(ms)
                    elif not in_override and was_active[name]:
                        logger.info(f"[{name}] Override session ended")
                        await self._session_end(ms)
                        self._stop_market_feeds(name)
                        ms.override_window = None
                    was_active[name] = ms.session_active
                    continue

                # Normal auto-activation based on day_type
                should_activate = now_in and win_idx is not None and self._should_auto_activate(ms, win_idx)

                if should_activate and not was_active[name]:
                    logger.info(f"[{name}] Session started")
                    ms.session_active = True
                    ms.state_mgr.reset_pnl()
                    self._start_market_feeds(name)
                    await self._dispatch_full_batch(ms)
                elif not should_activate and was_active[name]:
                    logger.info(f"[{name}] Session ended")
                    await self._session_end(ms)
                    self._stop_market_feeds(name)
                was_active[name] = ms.session_active

    def _start_market_feeds(self, market_name):
        loop = asyncio.get_event_loop()
        inv = self._inventory_feeds.get(market_name)
        if inv is not None:
            inv.start(loop)
            logger.info(f"[{market_name}] Inventory feed started")
        ms = self._markets.get(market_name)
        if ms and ms.config.alpha_enabled:
            alpha = self._alpha_feeds.get(market_name)
            if alpha is not None:
                alpha.start(loop)
                logger.info(f"[{market_name}] Alpha feed started")

    def _stop_market_feeds(self, market_name):
        inv = self._inventory_feeds.get(market_name)
        if inv is not None:
            inv.stop()
            logger.info(f"[{market_name}] Inventory feed stopped")
        ms = self._markets.get(market_name)
        if ms and ms.config.alpha_enabled:
            alpha = self._alpha_feeds.get(market_name)
            if alpha is not None:
                alpha.stop()
                logger.info(f"[{market_name}] Alpha feed stopped")

    async def _session_end(self, ms):
        ms.session_active = False
        cancel = build_cancel_all(ms.state_mgr)
        if not cancel.empty:
            ms.state_mgr.df["live_buy_qty"] = 0.0
            ms.state_mgr.df["live_sell_qty"] = 0.0
            out_df = dispatch_to_dataframe(cancel)
            logger.info(f"[{ms.name}] Session end: cancelling {len(cancel)} quotes")
            if self._dispatch_callback is not None:
                try:
                    self._dispatch_callback(out_df)
                except Exception:
                    logger.exception(f"[{ms.name}] Cancel-all dispatch failed")

    async def _process_command(self, cmd):
        action = cmd[0]
        market_name = cmd[1] if len(cmd) > 1 else None
        ms = self._markets.get(market_name)
        if action == "start" and ms:
            logger.info(f"[{market_name}] Command: start quoting")
            ms.quoting_enabled = True
            if not ms.session_active:
                now = now_hkt()
                cur_win = current_session_window(ms.config, now)
                if cur_win is not None:
                    ms.override_window = cur_win
                else:
                    nxt = next_session_window(ms.config, now)
                    if nxt is not None:
                        ms.override_window = nxt
                if ms.override_window is None:
                    logger.warning(f"[{market_name}] No session window available for override")
                else:
                    logger.info(f"[{market_name}] Override window set")
        elif action == "stop" and ms:
            logger.info(f"[{market_name}] Command: stop quoting")
            ms.quoting_enabled = False
        elif action == "reload" and ms:
            logger.info(f"[{market_name}] Command: reload config")
            if len(cmd) > 2:
                new_config = cmd[2]
            else:
                new_config = load_market_config(self._config_path, market_name)
                self._pimm_config = load_pimm_config(self._config_path)
            new_config.sessions = ms.config.sessions
            old_alpha = ms.config.alpha_enabled
            ms.config = new_config
            for ric in ms.state_mgr.df.index:
                ms.state_mgr.df.at[ric, "stock_limit"] = float(new_config.get_stock_limit(ric))
            if old_alpha and not new_config.alpha_enabled:
                alpha = self._alpha_feeds.get(market_name)
                if alpha is not None and ms.session_active:
                    alpha.stop()
                ms.state_mgr.df["alpha"] = 0.0
                logger.info(f"[{market_name}] Alpha disabled, zeroed")
            elif not old_alpha and new_config.alpha_enabled:
                alpha = self._alpha_feeds.get(market_name)
                if alpha is not None and ms.session_active:
                    alpha.start(asyncio.get_event_loop())
                logger.info(f"[{market_name}] Alpha enabled")
            logger.info(f"[{market_name}] Config reloaded")

    async def _delta_beta_loop(self):
        """Periodically query delta/beta info and update the engine state."""
        interval = self._pimm_config.delta_beta_interval_s
        while self._running:
            await asyncio.sleep(interval)
            try:
                info = _get_stub_delta_beta_info(self._markets)
                self.set_delta_beta_info(info)
            except Exception:
                logger.debug("Delta/beta query error", exc_info=True)

    async def _snapshot_loop(self):
        while self._running:
            await asyncio.sleep(0.1)
            if self._snapshot_callback is not None:
                try:
                    snap = self._build_snapshot()
                    await self._snapshot_callback(snap)
                except Exception:
                    logger.debug("Snapshot broadcast error", exc_info=True)

    def _build_snapshot(self):
        markets, scaling = {}, {}
        session_status, session_countdowns = {}, {}
        last_full_batch_times, market_configs = {}, {}
        day_types = {}
        for name, ms in self._markets.items():
            markets[name] = ms.state_mgr.copy()
            scaling[name] = (ms.buy_scaling, ms.sell_scaling)
            session_status[name] = ms.session_active
            session_countdowns[name] = seconds_until_session_end(ms.config)
            market_configs[name] = ms.config
            day_types[name] = ms.day_type
            if ms.last_full_batch_time is not None:
                last_full_batch_times[name] = ms.last_full_batch_time
        return EngineSnapshot(
            markets=markets, scaling=scaling, recent_fills=list(self._recent_fills), session_status=session_status,
            session_countdowns=session_countdowns, feed_status={}, timestamp=now_hkt(),
            last_full_batch_times=last_full_batch_times, delta_beta_info=self._delta_beta_info,
            console_log=list(self._console_log), market_configs=market_configs, day_types=day_types,
            pimm_config=self._pimm_config)
