# FastAPI web server — WebSocket dashboard, token auth

import json
import logging
import uuid
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from starlette.requests import Request

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(engine_ref, token):
    app = FastAPI()
    app.state.engine_ref = engine_ref
    app.state.token = token
    app.state.ws_clients = set()

    @app.get("/")
    async def index(request: Request):
        tok = request.query_params.get("token", "")
        if tok != app.state.token:
            return HTMLResponse("Unauthorized", status_code=403)
        return FileResponse(STATIC_DIR / "index.html")

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        tok = ws.query_params.get("token", "")
        if tok != app.state.token:
            await ws.close(code=4003)
            return

        await ws.accept()
        app.state.ws_clients.add(ws)
        client_ip = ws.client.host if ws.client else "unknown"
        logger.info(f"WebSocket client connected from {client_ip} ({len(app.state.ws_clients)} total)")

        try:
            while True:
                text = await ws.receive_text()
                try:
                    cmd = json.loads(text)
                    action = cmd.get("action")
                    market = cmd.get("market")
                    if action and market:
                        logger.info(f"Command '{action}' for [{market}] from {client_ip}")
                        ref = app.state.engine_ref
                        if ref.get("cmd_callback"):
                            ref["cmd_callback"](action, market)
                except (json.JSONDecodeError, KeyError):
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            app.state.ws_clients.discard(ws)
            logger.info(f"WebSocket client disconnected {client_ip} ({len(app.state.ws_clients)} remaining)")

    return app


def snapshot_to_json(snap):
    """Convert EngineSnapshot to JSON-serializable dict."""
    markets = {}
    for mkt, df in snap.markets.items():
        records = []
        for ric in df.index:
            r = df.loc[ric]
            sent = r["last_sent_time"]
            sent_str = sent.strftime("%H:%M:%S") if pd.notna(sent) else ""
            pnl_local = (r["last_price"] * (r["pnl_buy_qty"] - r["pnl_sell_qty"])
                         - r["pnl_buy_cost"] + r["pnl_sell_revenue"])
            pnl_usd = pnl_local * r["fx_rate"]
            buy_notional = r["pnl_buy_qty"] * r["last_price"]
            sell_notional = r["pnl_sell_qty"] * r["last_price"]
            max_not = max(buy_notional, sell_notional)
            min_not = min(buy_notional, sell_notional)
            ratio = (min_not / max_not * 100) if max_not > 0 else 0.0

            records.append(
                {
                    "ric": ric,
                    "quote_status": bool(r["quote_status"]),
                    "remark": (str(r["remark"]) if r["remark"] else ""),
                    "buy_state": (str(r["buy_state"]) if r["buy_state"] else ""),
                    "sell_state": (str(r["sell_state"]) if r["sell_state"] else ""),
                    "buy_raw": float(r["buy_raw"]),
                    "sell_raw": float(r["sell_raw"]),
                    "live_buy_qty": float(r["live_buy_qty"]),
                    "live_sell_qty": float(r["live_sell_qty"]),
                    "last_price": float(r["last_price"]),
                    "inventory": float(r["inventory"]),
                    "alpha": float(r["alpha"]),
                    "fx_rate": float(r["fx_rate"]),
                    "pnl_local": float(pnl_local),
                    "pnl_usd": float(pnl_usd),
                    "buy_notional": float(buy_notional),
                    "sell_notional": float(sell_notional),
                    "ratio": float(ratio),
                    "filled_buy": float(r["filled_buy_since_dispatch"]),
                    "filled_sell": float(r["filled_sell_since_dispatch"]),
                    "last_sent": sent_str,
                    "pnl_buy_qty": float(r["pnl_buy_qty"]),
                    "pnl_sell_qty": float(r["pnl_sell_qty"]),
                }
            )
        markets[mkt] = records

    scaling = {}
    for mkt, (bs, ss) in snap.scaling.items():
        scaling[mkt] = {"buy": bs, "sell": ss}

    fills = []
    for f in snap.recent_fills:
        fills.append(
            {
                "ric": f.ric,
                "side": f.side.value,
                "qty": f.fill_qty,
                "price": f.fill_price,
                "time": (f.timestamp.strftime("%H:%M:%S") if f.timestamp else ""),
            }
        )

    market_configs = {}
    for mkt, cfg in snap.market_configs.items():
        sessions_str = ", ".join(
            f"{s.start_hour:02d}:{s.start_minute:02d}-{s.end_hour:02d}:{s.end_minute:02d}" for s in cfg.sessions
        )
        market_configs[mkt] = {
            "sessions": sessions_str,
            "order_valid_time_m": cfg.order_valid_time_m,
            "refresh_buffer_s": cfg.refresh_buffer_s,
            "single_name_cap": cfg.single_name_cap,
            "max_buy_notional": cfg.max_buy_notional,
            "max_sell_notional": cfg.max_sell_notional,
            "partial_change_threshold": cfg.partial_change_threshold,
            "refill_fill_threshold": cfg.refill_fill_threshold,
            "alpha_enabled": cfg.alpha_enabled,
            "stock_limit_overrides": dict(cfg.stock_limit_overrides),
        }

    pimm_cfg = {}
    if snap.pimm_config is not None:
        pc = snap.pimm_config
        pimm_cfg = {"web_port": pc.web_port, "max_staleness_s": pc.max_staleness_s,
                     "full_batch_interval_m": pc.full_batch_interval_m,
                     "min_dispatch_interval_s": pc.min_dispatch_interval_s,
                     "delta_beta_interval_s": pc.delta_beta_interval_s}

    return {
        "markets": markets,
        "scaling": scaling,
        "fills": fills,
        "session_status": snap.session_status,
        "session_countdowns": dict(snap.session_countdowns),
        "pimm_config": pimm_cfg,
        "market_configs": market_configs,
        "day_types": dict(snap.day_types),
        "delta_beta_info": snap.delta_beta_info,
        "console_log": snap.console_log,
        "timestamp": (snap.timestamp.strftime("%H:%M:%S") if snap.timestamp else ""),
    }


async def broadcast_snapshot(app, snap):
    """Send snapshot to all connected WebSocket clients."""
    if not app.state.ws_clients:
        return
    data = json.dumps(snapshot_to_json(snap))
    dead = set()
    for ws in app.state.ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    app.state.ws_clients -= dead


def generate_token():
    return str(uuid.uuid4())
