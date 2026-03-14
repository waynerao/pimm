# pimm — Python Intelligent Market Maker

## 1. High-Level Overview

**Goal:** A multi-market dark pool market-making engine with dynamic pricing, controlled and monitored through a real-time web dashboard.

**Core Workflow:**
1. **Startup:** Start web server (FastAPI + WebSocket, same asyncio loop). Load config.cfg (all markets). For each market, load universe CSV and query lot sizes from desktool. Send access link via Outlook email.
2. **Trading Day Check:** Query desktool for each market's trading day type (non-trading / half / full). Auto-enable markets accordingly; web UI can override.
3. **State:** One universe DataFrame per market (one row per RIC) as the single source of truth for all per-stock state.
4. **Feeds:** Subscribe to shared feeds (Risk Appetite, Live Price, Fills) via desktool thread objects. Per-market feeds (Inventory, Alpha) start/stop with the market. Feed events routed to correct market by RIC.
5. **Quoting:** Compute sizes via a vectorized 4-step sizing pipeline. Apply inventory constraint at dispatch time. Dispatch in two modes: scheduled full batch and reactive partial update.
7. **Monitoring:** Web dashboard displays live quoting status, trade fills, PnL, delta/beta, console log, and provides controls for start/stop, parameter viewing/reloading.

## 2. Technical Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.11+ |
| Concurrency | `asyncio` (engine + web server) + `threading` (feeds) |
| Config | Single `config.cfg` via `configparser` + per-market universe `.csv` |
| Web UI | FastAPI + WebSocket + vanilla HTML/JS (served as static) |
| Email | `win32com.client` (Outlook COM) for sending access link |
| Alerting | `winsound.Beep` on feed staleness |
| Package mgmt | `uv` |
| Linting | `ruff` |
| Testing | `pytest` |

## 3. Configuration

### A. Config File (`configs/config.cfg`)

Uses Python `configparser` format. One section per market. All markets in a single file.

```ini
[HK]
universe_file = configs/hk_universe.csv
sessions = 09:30-12:00,13:00-16:00
order_valid_time = 5
refresh_buffer = 15
full_batch_interval = 10
min_dispatch_interval = 5
single_name_cap = 50000
max_buy_notional = 10000000
max_sell_notional = 10000000
max_staleness = 30
partial_change_threshold = 0.10
refill_fill_threshold = 0.50
alpha_enabled = true

[HK.overrides]
0005.HK = 100000
0700.HK = 20000

[TW]
universe_file = configs/tw_universe.csv
sessions = 09:00-13:30
alpha_enabled = false
...

[web]
port = 8080
recipients = user1@company.com,user2@company.com
delta_beta_interval = 5
```

Config loader (`pimm/config.py`) reads config.cfg, resolves per-stock overrides, and returns a config object per market.

### B. Stock Universe

Per-market CSV file (path in config) with a single `ric` column. All RICs from CSV are always included in the universe DataFrame — none are filtered out. Lot sizes queried from `desktool.get_lot_size()` at startup. RICs with no lot size remain in the DataFrame with `quote_status = False` and `remark = "no lot size"`. Only RICs with `quote_status = True` participate in the sizing pipeline and dispatch. The GUI displays `quote_status` and `remark` per stock.

### C. Trading Day Type

At startup, call `desktool.get_trading_day_type()` for each market. Returns:
- `1` — **Full day:** All configured sessions active.
- `0.5` — **Half day:** Morning session only (first session window).
- `0` — **Non-trading:** Market disabled by default.

GUI can override (manually enable/disable any market regardless of trading day type).

### D. Reload

The GUI provides a "Reload Params" button per country. Clicking it re-reads `config.cfg` from disk and applies updated values to the running engine for that market.

## 4. Universe DataFrame

All per-stock state lives in a single pandas DataFrame per market, indexed by `ric`. This is the only mutable state for the quoting engine.

**Columns:**

| Column | Type | Source | Description |
|--------|------|--------|-------------|
| `quote_status` | bool | startup / GUI | Whether this stock is actively quoting |
| `remark` | str | startup | Reason for status (e.g. "no lot size") |
| `lot_size` | int/NaN | desktool (startup) | Minimum tradeable lot (NaN if not found) |
| `stock_limit` | float | config (startup) | Single-name limit |
| `buy_state` / `sell_state` | str | risk appetite feed | Price type (BEST_BID / BEST_ASK) |
| `buy_raw` / `sell_raw` | float | risk appetite feed | Raw quantities from KDB+ |
| `last_price` | float | live price feed (KDB+ tick) | Stock price in local currency |
| `fx_rate` | float | risk appetite feed | Local currency → USD |
| `alpha` | float | alpha feed | Alpha signal ∈ [-1, 1] |
| `inventory` | float | inventory feed (per-market) | Current position |
| `live_buy_qty` / `live_sell_qty` | float | dispatch | Currently dispatched quantities |
| `last_sent_time` | datetime | dispatch | Last dispatch time |
| `filled_buy_since_dispatch` | float | fill event | Cumulative buy fills since last full batch |
| `filled_sell_since_dispatch` | float | fill event | Cumulative sell fills since last full batch |
| `pnl_buy_qty` | float | fill event | Cumulative bought qty today (reset on session start) |
| `pnl_buy_cost` | float | fill event | Cumulative buy cost today (fill_price × fill_qty) |
| `pnl_sell_qty` | float | fill event | Cumulative sold qty today (reset on session start) |
| `pnl_sell_revenue` | float | fill event | Cumulative sell revenue today (fill_price × fill_qty) |

**Thread safety:** No locks needed. Feed threads push into `asyncio.Queue` via `call_soon_threadsafe()`. Only the engine's single-threaded async loop reads/writes the DataFrame. Web UI receives copies via WebSocket snapshots.

## 5. Feed Interfaces

### A. Feed Adapter Pattern

All feeds use a queue-based adapter pattern via `FeedAdapter` base class.

**Shared desktool feeds** (Risk Appetite, Live Price, Fills): Receive a thread object from desktool. The base class stores the thread and controls its start/stop. Desktool pushes DataFrames into a `queue.Queue`; the adapter polls the queue and forwards each DataFrame to the engine's asyncio queue via `call_soon_threadsafe`. These run for the entire engine lifetime.

**Inventory feed** (`inventory.py`): One instance managing all countries. Each country has its own desktool thread subscription. Supports `start_market(market)` / `stop_market(market)` — subscriptions start/stop when the country is enabled/disabled.

**Alpha feed** (`alpha.py`): One instance managing all countries. Each country has its own queue — external alpha project pushes DataFrames per market. Supports `start_market(market)` / `stop_market(market)`. Controlled by `alpha_enabled` config flag (default `false`) — when disabled, no alpha subscription is started and alpha remains 0 (no skew).

**Simulator path**: Sim threads call `feed.on_update(df)` directly, bypassing the queue.

### B. Subscription Parameters

The `FeedAdapter` base class accepts these fields for configuring the desktool subscription:

- `data_queue` — `queue.Queue` for receiving DataFrames
- `service_name` — KDB+ service name
- `table_name` — KDB+ table name
- `recovery_query` — Query for initial state recovery
- `recovery_params` — Parameters for recovery query
- `filter_query` — Real-time filter query
- `filter_params` — Parameters for filter query

These are passed to the desktool thread function. Alpha feed does not use these fields.

### C. Feed Table

| Feed | Source | Per-Country Control | Columns |
|------|--------|---------------------|---------|
| Risk Appetite | desktool (KDB+) | No — always on | `ric, buy_state, buy_qty, sell_state, sell_qty, fx_rate` |
| Live Price | desktool (KDB+) | No — always on | `ric, last_price` |
| Trade Fills | desktool (KDB+) | No — always on | `ric, side, fill_qty, fill_price, timestamp` |
| Inventory | desktool (KDB+) | Yes — start/stop with country | `ric, inventory` |
| Alpha | external project | Yes — start/stop with country | `ric, alpha` (float in [-1, 1]) |

Shared feeds (Risk Appetite, Live Price, Fills) serve all markets; events routed by RIC membership. Inventory and Alpha each manage per-country subscriptions internally and tag events with market name.

### E. Delta/Beta Info (Periodic Query)

`desktool.get_delta_beta_info()` returns a single-line string with portfolio-level delta/beta information. Queried at a configurable interval (`delta_beta_interval` in config.cfg, default 5 seconds). This is global (not per-market) and displayed in the web dashboard's info panel.

## 6. Core Logic: The 4-Step Sizing Pipeline

All steps are vectorized pandas operations on the universe DataFrame — no per-stock loops. This produces the "optimal quote" — what we'd want to trade based on current inputs, unconstrained by inventory.

1. **Alpha Skew:** `buy *= (1 + alpha)`, `sell *= (1 - alpha)`. Alpha ∈ [-1, 1].
2. **Single-Name Limit:** `clip(upper=stock_limit)` per stock. Override via config, else market default `single_name_cap`.
3. **Notional Scaling:** Compute total buy/sell notional (USD). If over limit, scale all down proportionally. Scaling factors ∈ (0, 1].
4. **Lot Size Rounding:** `floor(qty / lot_size) * lot_size`.

**Inventory constraint** is applied separately at dispatch time (after scaling), not in the pipeline:
* `sell_dispatch = min(sell_optimal, max(0, inventory))` — no short selling.

## 7. Two-Mode Dispatch System

### A. Full Batch (Scheduled)
* Runs every `full_batch_interval` minutes (configurable, per market).
* Recomputes the entire 4-step pipeline on all stocks with fresh scaling factors.
* Applies inventory constraint.
* Dispatches all stocks. Updates live state.
* Resets `filled_buy/sell_since_dispatch` to 0 for all stocks.
* Saves scaling factors for partial updates to reuse.

### B. Partial Update (Reactive)
* Triggered by any feed change, subject to `min_dispatch_interval` cooldown (configurable, per market).
* Runs the 4-step pipeline using scaling factors from the last full batch.
* Applies inventory constraint.
* Selects stocks to dispatch based on two criteria:
  - **Quote change:** `|optimal - live| / live > partial_change_threshold` (e.g. 10%)
  - **Refill:** `filled_since_dispatch >= refill_fill_threshold * live_qty` (e.g. 50%)
* For refill stocks: dispatch qty capped at `optimal - filled_since_last_full_dispatch`.
* If partial would breach notional limit → auto-promotes to full batch.

### C. Dispatch Decision Flow
```
1. Cooldown active? → skip
2. Full batch due?  → full batch
3. Compute partial  → nothing qualifies? → skip
4. Notional check   → would breach? → full batch
5. Otherwise        → send partial
```

## 8. Dispatch Output

Each dispatch (full batch or partial update) produces a DataFrame representing the order to send. The output DataFrame contains only stocks being dispatched:

```
ric, buy_state, buy_qty, sell_state, sell_qty
```

Currently the output is logged/printed. The actual KDB+ injection via desktool will be wired later.

## 9. Refill Logic

Refill is one case of partial update, triggered when fills exceed a threshold.

* **On fill:** Accumulate `fill_qty` into `filled_buy/sell_since_dispatch`. Do NOT reduce `live_buy/sell_qty`.
* **Trigger:** `filled_since_dispatch >= refill_fill_threshold * live_qty` (e.g. 50% filled).
* **Amount:** Compute normal partial update quantity, then cap at `optimal - filled_since_last_full_dispatch`.
* **Reset:** `filled_since_dispatch` is only reset on full batch dispatch, not on partial.

## 10. Mark-to-Market PnL

Per-stock PnL is computed from aggregated fill data — no individual fill records stored.

### Accumulators (per stock, reset daily at session start)
* `pnl_buy_qty` / `pnl_buy_cost` — cumulative bought qty and cost (fill_price × fill_qty)
* `pnl_sell_qty` / `pnl_sell_revenue` — cumulative sold qty and revenue

### Formula
* **Local PnL:** `last_price × (pnl_buy_qty - pnl_sell_qty) - pnl_buy_cost + pnl_sell_revenue`
* **USD PnL:** `local_pnl × fx_rate`

This automatically re-evaluates when `last_price` changes — no recomputation loop needed.

### Daily Reset
All 4 accumulators are zeroed at session start (per market, when session becomes active).

## 11. Session Management

* **Session windows:** `"HH:MM-HH:MM"` (start inclusive, end exclusive). Multiple sessions per day for some markets.
* **Half trading day:** Only the first (morning) session is active.
* **Session start:** Immediate full batch dispatch.
* **Session end:** Cancel-all batch (zero-qty for all stocks).
* **Order validity:** `order_valid_time` (minutes) with `refresh_buffer` (seconds) for proactive refresh.

## 12. Web Dashboard

The web dashboard is the primary control and monitoring interface. It runs as a FastAPI server in the same asyncio loop as the engine (no separate process). Real-time updates are pushed via WebSocket.

For full layout, UI behavior, and implementation details, see **[web-dashboard.md](web-dashboard.md)**.

### Summary

- **Tech:** FastAPI + uvicorn (same asyncio loop) + WebSocket + vanilla HTML/JS single file
- **Auth:** UUID token in URL query param, validated on all requests, sent via Outlook email on startup
- **Layout:** Three vertical sections (Control+Summary | Delta/Beta+Log | Quoting/Trades tabs)
- **Features:** Draggable column reorder (persisted), resizable columns, resizable panels (flex-ratio based), country + RIC regex filters, dark theme
- **Naming:** Renamed offer→ask throughout (Bid State, Ask State, Opt Bid, Opt Ask, Live Bid, Live Ask)
- **Price types:** `BEST_BID` and `BEST_ASK` only (mid removed)
- **Coding style:** Compact PEP 8 (max 79 chars), f-strings for variable interpolation, grouped arguments

## 13. Engine Architecture

### A. Single Shared Process

One `asyncio` event loop manages the engine, all markets, and the web server. No multiprocessing — everything runs in a single process.

```
Single Process
├── asyncio event loop (main thread)
│   ├── TradingEngine.run()
│   │   ├── Per-market state (DataFrame, dispatch timing, session)
│   │   └── asyncio.Queue ← (event_type, market, DataFrame)
│   │
│   └── FastAPI (uvicorn)
│       ├── GET / → serves static HTML/JS
│       ├── WebSocket /ws?token={uuid}
│       │   ├── Server → Client: JSON snapshots (100ms)
│       │   └── Client → Server: commands (start/stop/reload)
│       └── Token validation middleware
│
├── Thread: feed-risk_appetite  (shared, desktool) ─┐
├── Thread: feed-live_price     (shared, desktool) ─┤ Always on, push via
├── Thread: feed-fills          (shared, desktool) ─┘ loop.call_soon_threadsafe()
│
├── InventoryFeed (1 instance, per-country desktool threads) ─┐ start/stop
├── AlphaFeed    (1 instance, per-country queues)             ─┘ with country
│
├── Thread: heartbeat-monitor   (daemon)
│
└── Startup: send email with access URL via win32com (Outlook)
```

### B. Feed Routing

Shared feeds push DataFrames containing RICs from multiple markets. The engine routes each row to the correct market's DataFrame based on RIC membership.

### C. Web Commands

The web UI sends commands to the engine via WebSocket messages:
- Start/stop quoting for a specific market
- Reload config for a specific market

## 14. Safety & Watchdog

* **Heartbeat:** Monitor KDB+ feed staleness. If `> max_staleness`, stop quoting and trigger `winsound`.
* **Session Termination:** On session end, send a "cancel all" (zero-size) batch per market.
* **Notional Limit:** Total dispatched notional (USD) per side must not exceed `max_buy/sell_notional`. Enforced by scaling in full batch and notional check in partial update.

## 15. Project Structure

```
pimarketmaker/
├── pimm.md                     # Project specification (this file)
├── web-dashboard.md            # Web dashboard layout & UI reference
├── PLAN.md                     # Implementation plan and progress
├── pyproject.toml              # Build config, deps, tool settings
├── configs/
│   ├── config.cfg              # All markets config (configparser format)
│   ├── hk_universe.csv         # HK stock universe
│   ├── cn_universe.csv         # CN stock universe
│   └── tw_universe.csv         # TW stock universe
├── pimm/
│   ├── __init__.py
│   ├── config.py               # Config loader (configparser + universe CSV)
│   ├── main.py                 # Production entry point
│   ├── simulator.py            # E2E simulation harness
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── state.py            # Per-market universe DataFrame management
│   │   ├── sizing.py           # 4-step sizing pipeline (vectorized pandas)
│   │   ├── dispatcher.py       # Full batch + partial update builder
│   │   ├── loop.py             # Async trading engine (shared loop, per-market state)
│   │   └── refill.py           # Fill accumulation + refill trigger logic
│   ├── feeds/
│   │   ├── __init__.py
│   │   ├── base.py             # FeedAdapter base (thread mgmt + queue polling → asyncio)
│   │   ├── risk_appetite.py    # Risk appetite feed (shared, desktool thread)
│   │   ├── live_price.py       # Live price feed (shared, desktool thread)
│   │   ├── fills.py            # Trade fills feed (shared, desktool thread)
│   │   ├── inventory.py        # Inventory feed (per-country start/stop, desktool threads)
│   │   ├── alpha.py            # Alpha signal feed (per-country start/stop, queues)
│   │   └── heartbeat.py        # Feed staleness monitor
│   ├── web/
│   │   ├── __init__.py
│   │   ├── server.py           # FastAPI app, WebSocket handler, token auth
│   │   ├── email.py            # Send access link via win32com (Outlook COM)
│   │   └── static/
│   │       └── index.html      # Dashboard UI (HTML + CSS + JS, single file)
│   └── utils/
│       ├── __init__.py
│       ├── quotetypes.py       # Shared data types (enums, TradeFill, EngineSnapshot)
│       └── time.py             # Timezone helpers + session checks
└── test/
    ├── __init__.py
    ├── conftest.py             # Shared fixtures
    ├── test_config.py          # Config loading + universe CSV
    ├── test_sizing.py          # Vectorized sizing pipeline
    ├── test_fills.py           # Fill accumulation + state updates
    ├── test_refill.py          # Refill trigger + capping logic
    ├── test_session.py         # Session windows + refresh timing
    ├── test_pnl.py             # PnL calculation + daily reset
    └── test_feeds.py           # Feed adapter base + concrete feeds
```

## 16. External Dependencies

| Package | Function | Description |
|---------|----------|-------------|
| **desktool** | `get_lot_size()` | Returns lot size dict for universe RICs |
| **desktool** | `get_trading_day_type()` | Returns `0` / `0.5` / `1` per market |
| **desktool** | `get_delta_beta_info()` | Returns single-line string with delta/beta info (polled periodically) |
| **desktool** | feed thread objects | Thread objects for risk appetite, live price, fills (shared) and inventory (per-market) subscriptions |
| **desktool** | quote injection | Send dispatch DataFrame to KDB+ (to be wired later) |
| **alpha project** | queue push | External project pushes alpha DataFrames into per-market queue |
| **fastapi** | web server | HTTP + WebSocket server for dashboard |
| **uvicorn** | ASGI server | Runs FastAPI in the asyncio loop |
| **win32com** | Outlook COM | Sends access link email on startup |
