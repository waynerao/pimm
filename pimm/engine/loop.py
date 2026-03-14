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

_EVT_RISK = "risk_appetite"
_EVT_LIVE_PRICE = "live_price"
_EVT_INVENTORY = "inventory"
_EVT_FILLS = "fills"
_EVT_ALPHA = "alpha"
_EVT_SHUTDOWN = "shutdown"
_EVT_COMMAND = "command"


class MarketState:

    def __init__(self, name, config, state_mgr):
        self.name, self.config = name, config
        self.state_mgr = state_mgr
        self.session_active = False
        self.quoting_enabled = True
        self.buy_scaling = self.sell_scaling = 1.0
        self.last_full_batch_time = None
        self.last_dispatch_time = None
        self.ric_set = set(state_mgr.df.index)


class TradingEngine:

    def __init__(self, market_states, dispatch_callback=None,
                 snapshot_callback=None, inventory_feed=None,
                 alpha_feed=None, config_path=None):
        self._markets = market_states
        self._dispatch_callback = dispatch_callback
        self._snapshot_callback = snapshot_callback
        self._inventory_feed = inventory_feed
        self._alpha_feed = alpha_feed
        self._config_path = config_path
        self._event_queue = asyncio.Queue()
        self._running = False
        self._recent_fills = []
        self._max_recent_fills = 50
        self._delta_beta_info = ""
        self._console_log = []
        self._max_console_log = 100
        self._ric_to_market = {}
        for mname, ms in self._markets.items():
            for ric in ms.ric_set:
                self._ric_to_market[ric] = mname

    @property
    def event_queue(self):
        return self._event_queue

    def push_event_threadsafe(self, loop, event_type, data):
        loop.call_soon_threadsafe(
            self._event_queue.put_nowait, (event_type, data)
        )

    def add_console_log(self, msg):
        self._console_log.append(msg)
        if len(self._console_log) > self._max_console_log:
            self._console_log = (
                self._console_log[-self._max_console_log:]
            )

    def set_delta_beta_info(self, info):
        self._delta_beta_info = info

    def process_web_command(self, action, market_name):
        self._event_queue.put_nowait(
            (_EVT_COMMAND, (action, market_name))
        )

    async def run(self):
        self._running = True
        logger.info(
            f"Trading engine started ({len(self._markets)} markets)"
        )
        session_task = asyncio.create_task(self._session_monitor())
        snapshot_task = asyncio.create_task(self._snapshot_loop())
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
                if event_type == _EVT_COMMAND:
                    await self._process_command(data)
                    continue
                await self._handle_event(event_type, data)
        finally:
            session_task.cancel()
            snapshot_task.cancel()
            for ms in self._markets.values():
                await self._session_end(ms)
            logger.info("Trading engine stopped")

    async def shutdown(self):
        self._running = False
        await self._event_queue.put((_EVT_SHUTDOWN, None))

    async def _handle_event(self, event_type, data):
        if event_type == _EVT_ALPHA:
            self._route_alpha(data)
        elif event_type in (
            _EVT_RISK, _EVT_LIVE_PRICE, _EVT_INVENTORY, _EVT_FILLS
        ):
            self._route_shared_feed(event_type, data)
        else:
            return
        affected = self._get_affected_markets(data)
        for mname in affected:
            ms = self._markets.get(mname)
            if ms and ms.session_active and ms.quoting_enabled:
                await self._try_dispatch(ms)

    def _route_shared_feed(self, event_type, data):
        if "ric" not in data.columns:
            return
        per_market = {}
        for _, row in data.iterrows():
            ric = str(row["ric"])
            mname = self._ric_to_market.get(ric)
            if mname is None:
                continue
            per_market.setdefault(mname, []).append(row)
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
            per_market.setdefault(mname, []).append(row)
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
            side = Side.BUY if side_str == "buy" else Side.SELL
            self._recent_fills.append(TradeFill(
                ric=ric, side=side, fill_qty=fill_qty,
                fill_price=fill_price, timestamp=ts,
            ))
        if len(self._recent_fills) > self._max_recent_fills:
            self._recent_fills = (
                self._recent_fills[-self._max_recent_fills:]
            )

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
        if (exp_buy > ms.config.max_buy_notional
                or exp_sell > ms.config.max_sell_notional):
            logger.info(
                f"[{ms.name}] Partial would breach notional "
                f"(buy={exp_buy:.0f} sell={exp_sell:.0f}), promoting"
            )
            await self._dispatch_full_batch(ms)
            return
        await self._dispatch_partial(ms, partial)

    async def _dispatch_full_batch(self, ms):
        dispatch, buy_s, sell_s = build_full_batch(
            ms.state_mgr, ms.config
        )
        ms.buy_scaling, ms.sell_scaling = buy_s, sell_s
        now_ts = now_hkt().timestamp()
        ms.last_full_batch_time = now_ts
        ms.last_dispatch_time = now_ts
        if dispatch.empty:
            return
        out_df = dispatch_to_dataframe(dispatch)
        logger.info(
            f"[{ms.name}] FULL BATCH: {len(dispatch)} quotes "
            f"(scaling buy={buy_s:.4f} sell={sell_s:.4f})"
        )
        if self._dispatch_callback is not None:
            try:
                self._dispatch_callback(out_df)
            except Exception:
                logger.exception(
                    f"[{ms.name}] Dispatch callback failed"
                )

    async def _dispatch_partial(self, ms, partial):
        apply_partial_to_state(ms.state_mgr, partial)
        ms.last_dispatch_time = now_hkt().timestamp()
        out_df = dispatch_to_dataframe(partial)
        logger.info(
            f"[{ms.name}] PARTIAL UPDATE: {len(partial)} quotes"
        )
        if self._dispatch_callback is not None:
            try:
                self._dispatch_callback(out_df)
            except Exception:
                logger.exception(
                    f"[{ms.name}] Dispatch callback failed"
                )

    async def _session_monitor(self):
        was_active = {name: False for name in self._markets}
        while self._running:
            await asyncio.sleep(1.0)
            for name, ms in self._markets.items():
                if not ms.quoting_enabled:
                    if ms.session_active:
                        logger.info(
                            f"[{name}] Quoting disabled, "
                            f"ending session"
                        )
                        await self._session_end(ms)
                        was_active[name] = False
                    continue
                now_active = is_in_session(ms.config)
                if now_active and not was_active[name]:
                    logger.info(f"[{name}] Session started")
                    ms.session_active = True
                    ms.state_mgr.reset_pnl()
                    self._start_market_feeds(name)
                    await self._dispatch_full_batch(ms)
                elif not now_active and was_active[name]:
                    logger.info(f"[{name}] Session ended")
                    await self._session_end(ms)
                    self._stop_market_feeds(name)
                was_active[name] = ms.session_active

    def _start_market_feeds(self, market_name):
        if self._inventory_feed is not None:
            self._inventory_feed.start_market(market_name)
        ms = self._markets.get(market_name)
        if (self._alpha_feed is not None
                and ms and ms.config.alpha_enabled):
            self._alpha_feed.start_market(market_name)

    def _stop_market_feeds(self, market_name):
        if self._inventory_feed is not None:
            self._inventory_feed.stop_market(market_name)
        ms = self._markets.get(market_name)
        if (self._alpha_feed is not None
                and ms and ms.config.alpha_enabled):
            self._alpha_feed.stop_market(market_name)

    async def _session_end(self, ms):
        ms.session_active = False
        cancel = build_cancel_all(ms.state_mgr)
        if not cancel.empty:
            ms.state_mgr.df["live_buy_qty"] = 0.0
            ms.state_mgr.df["live_sell_qty"] = 0.0
            out_df = dispatch_to_dataframe(cancel)
            logger.info(
                f"[{ms.name}] Session end: "
                f"cancelling {len(cancel)} quotes"
            )
            if self._dispatch_callback is not None:
                try:
                    self._dispatch_callback(out_df)
                except Exception:
                    logger.exception(
                        f"[{ms.name}] Cancel-all dispatch failed"
                    )

    async def _process_command(self, cmd):
        action = cmd[0]
        market_name = cmd[1] if len(cmd) > 1 else None
        ms = self._markets.get(market_name)
        if action == "start" and ms:
            logger.info(f"[{market_name}] Command: start quoting")
            ms.quoting_enabled = True
            if not ms.session_active and is_in_session(ms.config):
                logger.info(f"[{market_name}] Restarting session")
                ms.session_active = True
                self._start_market_feeds(market_name)
                await self._dispatch_full_batch(ms)
        elif action == "stop" and ms:
            logger.info(f"[{market_name}] Command: stop quoting")
            ms.quoting_enabled = False
        elif action == "reload" and ms:
            logger.info(f"[{market_name}] Command: reload config")
            if len(cmd) > 2:
                new_config = cmd[2]
            elif self._config_path:
                from pimm.config import reload_market_config
                new_config = reload_market_config(
                    self._config_path, market_name
                )
            else:
                logger.warning(
                    f"[{market_name}] No config path for reload"
                )
                return
            new_config.sessions = ms.config.sessions
            old_alpha = ms.config.alpha_enabled
            ms.config = new_config
            for ric in ms.state_mgr.df.index:
                ms.state_mgr.df.at[ric, "stock_limit"] = float(
                    new_config.get_stock_limit(ric)
                )
            if old_alpha and not new_config.alpha_enabled:
                if (self._alpha_feed is not None
                        and ms.session_active):
                    self._alpha_feed.stop_market(market_name)
                ms.state_mgr.df["alpha"] = 0.0
                logger.info(
                    f"[{market_name}] Alpha disabled, zeroed"
                )
            elif not old_alpha and new_config.alpha_enabled:
                if (self._alpha_feed is not None
                        and ms.session_active):
                    self._alpha_feed.start_market(market_name)
                logger.info(f"[{market_name}] Alpha enabled")
            logger.info(f"[{market_name}] Config reloaded")

    async def _snapshot_loop(self):
        while self._running:
            await asyncio.sleep(0.1)
            if self._snapshot_callback is not None:
                try:
                    snap = self._build_snapshot()
                    await self._snapshot_callback(snap)
                except Exception:
                    logger.debug(
                        "Snapshot broadcast error", exc_info=True
                    )

    def _build_snapshot(self):
        markets, scaling = {}, {}
        session_status, session_countdowns = {}, {}
        last_full_batch_times, market_configs = {}, {}
        for name, ms in self._markets.items():
            markets[name] = ms.state_mgr.copy()
            scaling[name] = (ms.buy_scaling, ms.sell_scaling)
            session_status[name] = ms.session_active
            session_countdowns[name] = (
                seconds_until_session_end(ms.config)
            )
            market_configs[name] = ms.config
            if ms.last_full_batch_time is not None:
                last_full_batch_times[name] = (
                    ms.last_full_batch_time
                )
        return EngineSnapshot(
            markets=markets, scaling=scaling,
            recent_fills=list(self._recent_fills),
            session_status=session_status,
            session_countdowns=session_countdowns,
            feed_status={}, timestamp=now_hkt(),
            last_full_batch_times=last_full_batch_times,
            delta_beta_info=self._delta_beta_info,
            console_log=list(self._console_log),
            market_configs=market_configs,
        )
