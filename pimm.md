# pimm — Python Intelligent Market Maker

## 1. High-Level Overview

**Goal:** A multi-market dark pool market-making engine with dynamic pricing, controlled and monitored through a real-time web dashboard.

**Core Workflow:**
1. **Startup:** Start web server (FastAPI + WebSocket, same asyncio loop). Load config TOML (general + market configs). For each market, load universe CSV; `StateManager` queries lot sizes from desktool internally. Send access link (LAN IP) via Outlook email.
2. **Trading Day Check:** Query desktool for each market's trading day type (non-trading / half / full). Auto-enable markets accordingly; web UI can override.
3. **State:** One universe DataFrame per market (one row per RIC) as the single source of truth for all per-stock state.
4. **Feeds:** Subscribe to shared feeds (Risk Appetite, Live Price, Fills) via desktool thread objects. Per-market feeds (Inventory, Alpha) are one instance per market, start/stop with the market. All feeds take a `ric_list` for subscription scope. Feed events broadcast to all markets; each market's `StateManager` self-filters by its own index.
5. **Quoting:** Compute sizes via a vectorized 4-step sizing pipeline. Apply inventory constraint at dispatch time. Dispatch in two modes: scheduled full batch and reactive partial update.
7. **Monitoring:** Web dashboard displays live quoting status, trade fills, PnL, delta/beta, console log, and provides controls for start/stop, parameter viewing/reloading.

## 2. Technical Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.12+ |
| Concurrency | `asyncio` (engine + web server) + `threading` (feeds) |
| Config | `config.toml` / `simulator.toml` via `tomllib` (stdlib) + per-market universe `.csv` |
| Web UI | FastAPI + WebSocket + vanilla HTML/JS (served as static) |
| Email | `win32com.client` (Outlook COM) for sending access link |
| Alerting | `winsound.Beep` on feed staleness |
| Package mgmt | `uv` |
| Linting | `ruff` |
| Testing | `pytest` |

## 3. Configuration

### A. Config File (`configs/config.toml`)

Single TOML file with three sections: general (`[pimm]`), shared market defaults (`[market_defaults]`), and per-market overrides (`[market.*]`). Uses `tomllib` (Python 3.12 stdlib).

Time field naming convention: suffix `_m` for minutes, `_s` for seconds.

```toml
[pimm]
web_port = 8080
max_staleness_s = 30
full_batch_interval_m = 10
min_dispatch_interval_s = 5
delta_beta_interval_s = 5
recipients = ["user1@company.com", "user2@company.com"]

[market_defaults]
order_valid_time_m = 5
refresh_buffer_s = 15
single_name_cap = 50000
max_buy_notional = 10000000
max_sell_notional = 10000000
partial_change_threshold = 0.10
refill_fill_threshold = 0.50
alpha_enabled = false

[market.HK]
universe_file = "configs/hk_universe.csv"
sessions = ["09:30-12:00", "13:00-16:00"]
alpha_enabled = true

[market.HK.overrides]
"0005.HK" = 100000
"0700.HK" = 20000

[market.TW]
universe_file = "configs/tw_universe.csv"
sessions = ["09:00-13:30"]
order_valid_time_m = 10
```

### A2. Config Classes

- **`PimmConfig`** — general settings: `web_port`, `max_staleness_s`, `full_batch_interval_m`, `min_dispatch_interval_s`, `delta_beta_interval_s`, `recipients`
- **`MarketConfig`** — per-market settings: `name`, `sessions`, `order_valid_time_m`, `refresh_buffer_s`, `single_name_cap`, `max_buy_notional`, `max_sell_notional`, `partial_change_threshold`, `refill_fill_threshold`, `universe_file`, `stock_limit_overrides`, `alpha_enabled`

### A3. Defaults Mechanism

Config loader merges `[market_defaults]` with each `[market.*]` table — per-market values win. Any field in `[market_defaults]` can be overridden per market. Fields not present in either get hardcoded defaults.

### B. Stock Universe

Per-market CSV file (path in config) with a single `ric` column. All RICs from CSV are always included in the universe DataFrame — none are filtered out. Lot sizes queried from `desktool.get_lot_size()` at startup. RICs with no lot size remain in the DataFrame with `quote_status = False` and `remark = "no lot size"`. Only RICs with `quote_status = True` participate in the sizing pipeline and dispatch. The GUI displays `quote_status` and `remark` per stock.

### C. Trading Day Type

At startup, call `desktool.get_trading_day_type(market)` for each market. Returns:
- `1` — **Full day:** All configured sessions active. Per-market feeds (inventory, alpha) start automatically on session entry.
- `0.5` — **Half day:** Only the first session window auto-activates. After first session ends, market stays inactive — session monitor does not auto-activate subsequent windows. Per-market feeds start on first session entry, stop when it ends.
- `0` — **Non-trading:** No sessions auto-activate. Per-market feeds are not started.

The `day_type` value only controls **initialization and auto-activation**. The web UI can override:
- **Start button** on `day_type == 0` or on `day_type == 0.5` past the first window triggers a confirmation warning in the frontend. On confirm, the engine activates the current or next upcoming session window as `override_window`, and starts per-market feeds.
- The override applies to a single window only — after that window ends, market goes inactive again unless the user overrides again.

### D. Reload

The web UI provides a "Reload Params" button per country. Clicking it re-reads the config TOML from disk and applies updated values to the running engine for that market. `TradingEngine` stores the `config_path` and uses it for reload via `load_market_config()` + `load_pimm_config()`.

## 4. Universe DataFrame

All per-stock state lives in a single pandas DataFrame per market, indexed by `ric`. This is the only mutable state for the quoting engine. `StateManager(ric_list, config)` builds the DataFrame: loads universe from CSV, queries lot sizes from desktool internally (vectorized via `index.map`), sets `quote_status` and `remark` vectorially, and logs the universe summary. All RICs are always included — missing lot sizes only affect `quote_status`, never universe membership.

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

**Feed updates:** All `update_*()` methods are vectorized and self-filtering. They receive the full feed DataFrame, filter to rows matching their own index via `df.index.intersection(self.df.index)`, update matching rows, and return `True` if any rows matched. The engine uses this return value to determine which markets need dispatch.

**Thread safety:** No locks needed. Feed threads push into `asyncio.Queue` via `call_soon_threadsafe()`. Only the engine's single-threaded async loop reads/writes the DataFrame. Web UI receives copies via WebSocket snapshots.

## 5. Feed Interfaces

### A. Feed Adapter Pattern

All feeds share the same `FeedAdapter` base class. Every feed receives a `ric_list` at init, defining the stock universe it subscribes to.

**Init parameters (base class):**
- `event_type` — event name for engine routing
- `engine_push` — callback to push (event_type, DataFrame) to engine
- `ric_list` — stock universe for this feed's subscription scope
- `thread` — desktool thread object (optional)
- `service_name`, `table_name` — KDB+ subscription config
- `recovery_query` / `recovery_params` — initial state recovery
- `filter_query` / `filter_params` — real-time filter

**Lifecycle:** `start(loop)` starts the desktool thread + queue polling. `stop()` stops both. When stopped, `_push()` is a no-op — sim threads can keep running but data goes nowhere and no logging occurs. Push logging (`Feed {type}: pushed {n} rows`) only happens in `_push()` when the feed is active. All concrete feed classes (`RiskAppetiteFeed`, `LivePriceFeed`, `FillsFeed`, `InventoryFeed`, `AlphaFeed`) are subclasses of `FeedAdapter` with the same interface — kept as separate classes for future per-feed customization.

**Shared feeds** (Risk Appetite, Live Price, Fills): One instance each, `ric_list` = all RICs across all markets. Run for the entire engine lifetime.

**Per-market feeds** (Inventory, Alpha): **One instance per market** — both take all RICs for the market (not just quotable). e.g. `InventoryFeed(ric_list=hk_rics, ...)`, `AlphaFeed(ric_list=hk_rics, ...)`. The engine manages them as dicts: `inventory_feeds["HK"]`, `alpha_feeds["HK"]`. Start/stop is managed by the engine's session monitor — feeds start on session start and stop on session end or when quoting is disabled via the Stop button. Alpha feeds are only created when `alpha_enabled = true` for that market. Non-quotable RICs are harmlessly ignored by `update_*()` self-filtering.

**Simulator path:** Sim threads call `feed.on_update(df)` directly, bypassing the queue.

### B. Feed Event Routing

The engine does **not** route feed events by RIC. On any feed event, the engine passes the full DataFrame to every market's `StateManager`. Each `StateManager.update_*()` method self-filters by its own index (vectorized intersection) and returns `True` if any rows matched. The engine dispatches only markets that had matching data.

### C. Feed Table

| Feed | Source | Instances | Columns |
|------|--------|-----------|---------|
| Risk Appetite | desktool (KDB+) | 1 (shared, all RICs) | `ric, buy_state, buy_qty, sell_state, sell_qty, fx_rate` |
| Live Price | desktool (KDB+) | 1 (shared, all RICs) | `ric, last_price` |
| Trade Fills | desktool (KDB+) | 1 (shared, all RICs) | `ric, side, fill_qty, fill_price, timestamp` |
| Inventory | desktool (KDB+) | 1 per market | `ric, inventory` |
| Alpha | external project | 1 per market (if `alpha_enabled`) | `ric, alpha` (float in [-1, 1]) |

### E. Delta/Beta Info (Periodic Query)

`desktool.get_delta_beta_info()` returns a single-line string with portfolio-level delta/beta information. Queried at a configurable interval (`delta_beta_interval_s` in `[pimm]` section, default 5 seconds). This is global (not per-market) and displayed in the web dashboard's info panel.

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
* Runs every `full_batch_interval_m` minutes (configurable in `[pimm]`).
* Recomputes the entire 4-step pipeline on all stocks with fresh scaling factors.
* Applies inventory constraint.
* Dispatches all stocks. Updates live state.
* Resets `filled_buy/sell_since_dispatch` to 0 for all stocks.
* Saves scaling factors for partial updates to reuse.

### B. Partial Update (Reactive)
* Triggered by any feed change, subject to `min_dispatch_interval_s` cooldown (configurable in `[pimm]`).
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
* **Order validity:** `order_valid_time_m` (minutes) with `refresh_buffer_s` (seconds) for proactive refresh.
* **Session start:** Immediate full batch dispatch + PnL reset + start per-market feeds.
* **Session end:** Cancel-all batch (zero-qty for all stocks) + stop per-market feeds.
* **Stop button:** Disabling quoting via web UI also ends session + stops per-market feeds.

### Trading Day Type Behavior

`MarketState` stores `day_type` (0, 0.5, 1) from `desktool.get_trading_day_type(market)` at init. The session monitor uses this flag:

| `day_type` | Auto-activate windows | Per-market feeds |
|---|---|---|
| `1` (full) | All windows | Start/stop with each window |
| `0.5` (half) | First window only | Start on first window, stop when it ends |
| `0` (non-trading) | None | Not started |

### User Override (Start Button)

The web UI Start button can override `day_type` restrictions:
- `day_type == 0`: always shows a confirmation warning before starting
- `day_type == 0.5` and past first window: shows a confirmation warning
- `day_type == 1` or `day_type == 0.5` within first window: no warning

On confirm, the engine identifies the **current or next upcoming session window** and sets it as `override_window`. That single window runs normally (full batch on start, cancel-all on end, per-market feeds active). After the window ends, the market goes inactive again — no further auto-activation unless the user overrides again.

## 12. Web Dashboard

The web dashboard is the primary control and monitoring interface. It runs as a FastAPI server in the same asyncio loop as the engine (no separate process). Real-time updates are pushed via WebSocket.

For full layout, UI behavior, and implementation details, see **[web-dashboard.md](web-dashboard.md)**.

### Summary

- **Tech:** FastAPI + uvicorn (same asyncio loop) + WebSocket + vanilla HTML/JS single file
- **Auth:** UUID token in URL query param, validated on all requests, sent via Outlook email on startup
- **Layout:** Three vertical sections (Control+Summary | Delta/Beta+Log | Quoting/Trades tabs)
- **Features:** Draggable column reorder (persisted), resizable columns, resizable panels (flex-ratio based), country + RIC regex filters, log level dropdown + regex text filter, delta/beta regex text filter, dark theme
- **Logging:** `WebLogHandler` captures INFO+ as structured `{level, msg}` entries (buffer 2000); frontend filters by level (DEBUG→CRITICAL) and regex; color-coded (gray/white/yellow/red+bold)
- **File logging:** `logs/pimm_{YYYYMMDD}.log` (production) / `logs/sim_{YYYYMMDD_HHMMSS}.log` (simulator), DEBUG level, full history
- **Audit:** WebSocket commands logged with client IP (connect, disconnect, start/stop/reload)
- **Naming:** Renamed offer→ask throughout (Bid State, Ask State, Opt Bid, Opt Ask, Live Bid, Live Ask)
- **Price types:** `BEST_BID` and `BEST_ASK` only (mid removed)
- **Coding style:** Compact style (max 120 chars), f-strings for variable interpolation, packed arguments

## 13. Engine Architecture

### A. Single Shared Process

One `asyncio` event loop manages the engine, all markets, and the web server. No multiprocessing — everything runs in a single process. `TradingEngine` takes a `config_path` and loads its own `PimmConfig` internally.

```
Single Process
├── asyncio event loop (main thread)
│   ├── TradingEngine.run(config_path)
│   │   ├── Loads PimmConfig from config_path
│   │   ├── Per-market state (DataFrame, dispatch timing, session, day_type)
│   │   └── asyncio.Queue ← (event_type, DataFrame)
│   │
│   └── FastAPI (uvicorn)
│       ├── GET / → serves static HTML/JS
│       ├── WebSocket /ws?token={uuid}
│       │   ├── Server → Client: JSON snapshots (100ms)
│       │   └── Client → Server: commands (start/stop/reload)
│       └── Token validation middleware
│
├── Thread: feed-risk_appetite  (shared, all RICs)  ─┐
├── Thread: feed-live_price     (shared, all RICs)  ─┤ Always on, push via
├── Thread: feed-fills          (shared, all RICs)  ─┘ loop.call_soon_threadsafe()
│
├── Per-market: InventoryFeed (1 per market)  ─┐ start/stop
├── Per-market: AlphaFeed     (1 per market)  ─┘ with market session
│
├── Thread: heartbeat-monitor   (daemon)
│
└── Startup: send email with access URL via win32com (Outlook)
```

### B. Feed Event Handling

On any feed event, the engine broadcasts the full DataFrame to every market's `StateManager`. Each `StateManager.update_*()` self-filters by its own index (vectorized) and returns `True` if any rows matched. The engine dispatches only markets that had matching data. No RIC-to-market routing map needed.

### C. Web Commands

The web UI sends commands to the engine via WebSocket messages:
- Start/stop quoting for a specific market
- Reload config for a specific market

## 14. Safety & Watchdog

* **Heartbeat:** Monitor KDB+ feed staleness. If `> max_staleness_s`, stop quoting and trigger `winsound`.
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
│   ├── config.toml             # Production config: [pimm] + [market_defaults] + [market.*]
│   ├── simulator.toml          # Simulator config: fast batches, all-day sessions, lower notional
│   ├── hk_universe.csv         # HK stock universe
│   ├── cn_universe.csv         # CN stock universe
│   └── tw_universe.csv         # TW stock universe
├── pimm/
│   ├── __init__.py
│   ├── config.py               # Config loader (tomllib + universe CSV)
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
│       ├── network.py          # LAN IP / hostname resolution for dashboard URL
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
