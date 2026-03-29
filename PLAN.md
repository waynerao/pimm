# PLAN.md — pimm (Python Intelligent Market Maker)

## 1. Project Overview

A multi-market dark pool market-making engine with dynamic pricing, controlled and monitored through a real-time GUI dashboard.

**Core workflow:**
1. Start web server (FastAPI + WebSocket) — the primary control and monitoring interface
2. Send access link via Outlook email (auto-send on startup, configurable recipients)
3. Load config.toml (all markets); per market: load universe CSV, query lot sizes from desktool
4. Check trading day type per market (desktool); auto-enable/disable accordingly
5. Initialize per-market universe DataFrame — single source of truth for all per-stock state
6. Subscribe to shared feeds (risk appetite, live price, fills) via desktool threads
7. Subscribe to per-market feeds (inventory, alpha) — start/stop with each country
8. Compute quote sizes through a vectorized 4-step sizing pipeline
9. Dispatch quotes in two modes: scheduled full batch and reactive partial update
10. Web dashboard displays live status, trade fills, PnL, delta/beta, console log; provides start/stop, param view/reload controls

---

## 2. How the Market Maker Works

### 2.1 Startup Sequence

```
1. Load config TOML → PimmConfig + MarketConfig per market ([market.HK], [market.TW], etc.)
2. For each market:
   a. Query desktool.get_trading_day_type(market) → 0 / 0.5 / 1
   b. Load universe CSV → list of RICs
   c. StateManager(ric_list, config) — queries lot sizes from desktool internally
      - Vectorized init: index.map for lot sizes, notna for quote_status
      - RICs with no lot size → quote_status=False, remark="no lot size"
      - Logs universe summary + missing lot sizes
   d. Set day_type on MarketState:
      - 1 (full): all sessions auto-activate
      - 0.5 (half): only first session auto-activates
      - 0 (non-trading): no sessions auto-activate
3. Create shared feeds (risk appetite, live price, fills)
   - ric_list = all RICs across all markets
4. Create per-market feeds (inventory, alpha — one instance per market)
   - ric_list = all RICs for that market (not just quotable)
   - Alpha feed only created if alpha_enabled = true
   - NOT started yet — start/stop with market session
5. Start shared feeds + heartbeat monitor
6. Start FastAPI web server (same asyncio loop)
7. Generate token, resolve LAN IP via `get_host_ip()`, send access link via Outlook email
8. Start shared async engine loop (manages all markets)
9. Start periodic delta/beta info query (configurable interval)
```

### 2.2 Universe DataFrame (per market)

All per-stock state lives in a single pandas DataFrame per market, indexed by `ric`.

**Columns:**

| Column | Source | Updated by | Description |
|--------|--------|------------|-------------|
| `ric` | CSV | startup | Stock identifier (index) |
| `quote_status` | startup / GUI | startup | Whether this stock is actively quoting (bool) |
| `remark` | startup | startup | Reason for status (e.g. "no lot size") |
| `lot_size` | desktool | startup | Minimum tradeable lot (NaN if not found) |
| `stock_limit` | config | startup | Single-name limit (per-stock override or market default) |
| `buy_state` | risk appetite feed | `update_risk_appetite()` | Price type for buy side |
| `sell_state` | risk appetite feed | `update_risk_appetite()` | Price type for sell side |
| `buy_raw` | risk appetite feed | `update_risk_appetite()` | Raw buy quantity from KDB+ |
| `sell_raw` | risk appetite feed | `update_risk_appetite()` | Raw sell quantity from KDB+ |
| `last_price` | live price feed | `update_live_price()` | Stock price in local currency |
| `fx_rate` | risk appetite feed | `update_risk_appetite()` | Local currency → USD |
| `alpha` | alpha feed | `update_alpha()` | Alpha signal ∈ [-1, 1] |
| `inventory` | inventory feed (per-market) | `update_inventory()` | Current position |
| `live_buy_qty` | dispatch | `_dispatch_*()` | Currently dispatched buy quantity |
| `live_sell_qty` | dispatch | `_dispatch_*()` | Currently dispatched sell quantity |
| `last_sent_time` | dispatch | `_dispatch_*()` | Timestamp of last dispatch |
| `filled_buy_since_dispatch` | fill event | `accumulate_fills()` | Cumulative buy fills since last full batch |
| `filled_sell_since_dispatch` | fill event | `accumulate_fills()` | Cumulative sell fills since last full batch |
| `pnl_buy_qty` | fill event | `_handle_fills()` | Cumulative bought qty today |
| `pnl_buy_cost` | fill event | `_handle_fills()` | Cumulative buy cost today |
| `pnl_sell_qty` | fill event | `_handle_fills()` | Cumulative sold qty today |
| `pnl_sell_revenue` | fill event | `_handle_fills()` | Cumulative sell revenue today |

**Thread safety:** No locks needed. Feed threads push into `asyncio.Queue` via `call_soon_threadsafe()`. Only the engine's async loop reads/writes DataFrames. Web UI receives copies via WebSocket snapshots.

### 2.3 Steady-State Event Loop

```
Feed event arrives (risk appetite / live price / inventory / alpha / fill)
  │
  ├─ Broadcast full DataFrame to every market's StateManager
  │   └─ Each StateManager.update_*() self-filters by own index (vectorized)
  │      returns True if any rows matched
  │
  ├─ For each market that had matching data:
  │   └─ If session is active → run dispatch decision logic
  │
  └─ Push snapshot via WebSocket (copies of all market DataFrames, non-blocking)
```

### 2.4 Dispatch Decision Logic (per market)

```
_try_dispatch(market):
  │
  ├─ 1. COOLDOWN CHECK
  │     Was last dispatch < min_dispatch_interval_s ago?
  │     → YES: do nothing
  │
  ├─ 2. FULL BATCH CHECK
  │     Has it been >= full_batch_interval_m since last full batch?
  │     → YES: run full batch
  │
  ├─ 3. COMPUTE PARTIAL (vectorized)
  │     Run 4-step pipeline using last batch's scaling
  │     Apply inventory constraint
  │     Filter stocks needing update:
  │       Case A — Quote change: |optimal - live| / live > partial_change_threshold
  │       Case B — Refill: filled >= refill_fill_threshold * live_qty
  │     Cap refill stocks: dispatch qty ≤ optimal - filled_since_last_full_dispatch
  │     → Empty: do nothing
  │
  ├─ 4. NOTIONAL CHECK
  │     Would push total notional over limit?
  │     → YES: promote to full batch
  │
  └─ 5. SEND PARTIAL
        Dispatch only the selected stocks
```

### 2.5 The 4-Step Sizing Pipeline (Vectorized)

```python
# Step 1: Alpha Skew
df['buy_skewed']  = df['buy_raw'] * (1 + df['alpha'])
df['sell_skewed'] = df['sell_raw'] * (1 - df['alpha'])

# Step 2: Single-Name Cap
df['buy_capped']  = df['buy_skewed'].clip(upper=df['stock_limit'])
df['sell_capped'] = df['sell_skewed'].clip(upper=df['stock_limit'])

# Step 3: Notional Scaling
buy_notional  = (df['buy_capped'] * df['last_price'] * df['fx_rate']).sum()
sell_notional = (df['sell_capped'] * df['last_price'] * df['fx_rate']).sum()
buy_scaling   = min(1.0, max_buy_notional / buy_notional)  if buy_notional > 0 else 1.0
sell_scaling  = min(1.0, max_sell_notional / sell_notional) if sell_notional > 0 else 1.0
df['buy_scaled']  = df['buy_capped'] * buy_scaling
df['sell_scaled'] = df['sell_capped'] * sell_scaling

# Step 4: Lot Rounding
df['buy_optimal']  = (df['buy_scaled']  // df['lot_size']) * df['lot_size']
df['sell_optimal'] = (df['sell_scaled'] // df['lot_size']) * df['lot_size']
```

### 2.6 Pre-Dispatch: Inventory Constraint

```python
df['sell_dispatch'] = df['sell_optimal'].clip(upper=df['inventory'].clip(lower=0))
df['buy_dispatch']  = df['buy_optimal']
```

### 2.7 Full Batch vs Partial Update

**Full Batch** (scheduled, every `full_batch_interval_m` minutes per market):
- Recalculates global scaling factors from scratch
- Dispatches all stocks with non-zero quantities
- Resets `filled_buy/sell_since_dispatch` to 0
- Saves scaling factors for partial updates

**Partial Update** (reactive, between full batches):
- Uses cached scaling factors from last full batch
- Selects stocks by quote change or refill threshold
- Refill cap: `optimal - filled_since_last_full_dispatch`
- Auto-promotes to full batch on notional breach

### 2.8 Refill Logic

- **On fill:** Accumulate into `filled_buy/sell_since_dispatch` (do NOT reduce live qty)
- **Trigger:** `filled >= refill_fill_threshold * live_qty`
- **Cap:** `dispatch_qty = min(optimal, optimal - filled_since_last_full_dispatch)`
- **Reset:** Only on full batch, not on partial

### 2.9 Dispatch Output

Each dispatch (full batch or partial update) produces a DataFrame representing the order:

```
ric, buy_state, buy_qty, sell_state, sell_qty
```

Currently logged/printed. Actual KDB+ injection via desktool to be wired later.

### 2.10 Mark-to-Market PnL

**Accumulators** (per stock, reset daily at session start):
- `pnl_buy_qty` / `pnl_buy_cost` — cumulative bought qty and cost
- `pnl_sell_qty` / `pnl_sell_revenue` — cumulative sold qty and revenue

**Formula:**
- Local PnL: `last_price × (pnl_buy_qty - pnl_sell_qty) - pnl_buy_cost + pnl_sell_revenue`
- USD PnL: `local_pnl × fx_rate`

Auto re-evaluates when `last_price` changes. Reset at session start per market.

### 2.11 Session Management

- **Session windows:** `"HH:MM-HH:MM"` per market (multiple allowed)
- **Order validity:** `order_valid_time_m` minutes with `refresh_buffer_s` seconds
- **Session start:** Start per-market feeds + immediate full batch dispatch + PnL reset
- **Session end:** Cancel-all batch (zero-qty for all stocks) + stop per-market feeds

### 2.12 Trading Day Type & Session Monitor

`MarketState` stores `day_type` (0, 0.5, 1) from `desktool.get_trading_day_type(market)`.

**Session monitor auto-activation rules:**

| `day_type` | Auto-activate | After first window ends |
|---|---|---|
| `1` (full) | All windows | Continues to next window |
| `0.5` (half) | First window only | Market stays inactive |
| `0` (non-trading) | None | N/A |

**User override (Start button in web UI):**
- Warning popup shown when: `day_type == 0`, or `day_type == 0.5` past the first window
- No warning when: `day_type == 1`, or `day_type == 0.5` within/before the first window
- On confirm: engine sets `override_window` to the current or next upcoming session window
- Override applies to **one window only** — after it ends, market goes inactive again
- All session windows remain in config (not trimmed) — `day_type` flag controls behavior

---

## 3. Configuration

### 3.1 Config File (`configs/config.toml`)

Uses Python `tomllib` (stdlib). Single TOML file with three sections: general (`[pimm]`), shared market defaults (`[market_defaults]`), and per-market overrides (`[market.*]`).

Time field naming convention: suffix `_m` for minutes, `_s` for seconds.

```toml
[pimm]
web_port = 8080
max_staleness_s = 30
full_batch_interval_m = 10
min_dispatch_interval_s = 5
delta_beta_interval_s = 5
recipients = ["user1@company.com"]

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

### 3.2 Config Classes

- **`PimmConfig`** — general project settings: `web_port`, `max_staleness_s`, `full_batch_interval_m`, `min_dispatch_interval_s`, `delta_beta_interval_s`, `recipients`
- **`MarketConfig`** — per-market settings: `name`, `sessions`, `order_valid_time_m`, `refresh_buffer_s`, `single_name_cap`, `max_buy_notional`, `max_sell_notional`, `partial_change_threshold`, `refill_fill_threshold`, `universe_file`, `stock_limit_overrides`, `alpha_enabled`

### 3.3 Config Loader (`pimm/config.py`)

- Reads `config.toml` using `tomllib`
- Merges `[market_defaults]` with each `[market.*]` table (per-market wins)
- Returns `PimmConfig` for general settings, `MarketConfig` per market
- Resolves per-stock overrides from `[market.{name}.overrides]`
- Supports reload at runtime (triggered by web UI button)

### 3.3 Trading Day Type

`desktool.get_trading_day_type()` returns per-market trading day type at startup:
- `1` — **Full:** All sessions active
- `0.5` — **Half:** Morning session only
- `0` — **Non-trading:** Disabled (GUI can override)

---

## 4. Feed Interfaces

### 4.1 Feed Adapter Base Class

All feeds share the `FeedAdapter` base class. Every feed receives a `ric_list` at init defining its subscription scope.

**Init parameters:**
- `event_type` — event name for engine routing
- `engine_push` — callback to push (event_type, DataFrame) to engine
- `ric_list` — stock universe for this feed's subscription
- `market_name` — market identifier for logging (default "ALL" for shared feeds)
- `thread` — desktool thread object (optional)
- `service_name`, `table_name` — KDB+ subscription config
- `recovery_query` / `recovery_params` — initial state recovery
- `filter_query` / `filter_params` — real-time filter

**Lifecycle:** `start(loop)` / `stop()` — controls the whole instance.
**Simulator:** calls `feed.on_update(df)` directly (bypasses queue).
**Subclasses:** `RiskAppetiteFeed`, `LivePriceFeed`, `FillsFeed`, `InventoryFeed`, `AlphaFeed` — kept as separate classes for future per-feed customization.

### 4.2 Feed Instances

**Shared feeds** (Risk Appetite, Live Price, Fills): One instance each, `ric_list` = all RICs across all markets. Run for the entire engine lifetime.

**Per-market feeds** (Inventory, Alpha): One instance per market. `ric_list` = that market's RICs. Engine stores them as dicts: `inventory_feeds["HK"]`, `alpha_feeds["HK"]`. Start/stop with market session — just `feed.start(loop)` / `feed.stop()` on each instance.

### 4.3 Feed Event Routing

The engine does **not** route feed events by RIC. On any feed event, the engine passes the full DataFrame to every market's `StateManager`. Each `update_*()` method self-filters by its own index (vectorized intersection) and returns `True` if any rows matched. Only markets with matching data trigger dispatch.

### 4.4 Feed Table

| Feed | Source | Instances | Columns |
|------|--------|-----------|---------|
| Risk Appetite | desktool (KDB+) | 1 (shared, all RICs) | `ric, buy_state, buy_qty, sell_state, sell_qty, fx_rate` |
| Live Price | desktool (KDB+) | 1 (shared, all RICs) | `ric, last_price` |
| Trade Fills | desktool (KDB+) | 1 (shared, all RICs) | `ric, side, fill_qty, fill_price, timestamp` |
| Inventory | desktool (KDB+) | 1 per market | `ric, inventory` |
| Alpha | external project | 1 per market (if `alpha_enabled`) | `ric, alpha` (float in [-1, 1]) |

### 4.4 Delta/Beta Info (Periodic Query)

`desktool.get_delta_beta_info()` — global, returns a single-line string. Polled at `delta_beta_interval_s` seconds (configurable in `[pimm]` section). Displayed in web dashboard's info panel.

---

## 5. Web Dashboard

Full layout and UI details are documented in **[web-dashboard.md](web-dashboard.md)**.

**Summary of key features:**
- FastAPI + uvicorn in same asyncio loop, WebSocket for real-time data + commands
- Token-based auth (UUID in URL), sent via Outlook email on startup
- Three-section layout: Control+Summary | Delta/Beta+Log | Quoting+Trades tabs
- Draggable column reordering (persisted to localStorage)
- Resizable columns and panels (flex-ratio based)
- Country dropdown + RIC regex filters
- RIC sorting by region then alphabetically
- Dark theme, single-file HTML/CSS/JS

---

## 6. Architecture

### 6.1 Concurrency

```
Single Process
├── asyncio event loop (main thread)
│   ├── TradingEngine.run()
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
├── Thread: heartbeat-monitor    (daemon)
│
└── Startup: send email with access URL via win32com (Outlook)
```

### 6.2 Feed Event Handling

On any feed event, the engine broadcasts the full DataFrame to every market's `StateManager`. Each `update_*()` self-filters by its own index (vectorized) and returns `True` if matched. Only affected markets trigger dispatch. No RIC-to-market routing map.

### 6.3 Web Commands

Web UI sends commands to engine via WebSocket messages:
- `{"action": "start", "market": "HK"}` — start quoting for a market
- `{"action": "stop", "market": "HK"}` — stop quoting for a market
- `{"action": "reload", "market": "HK"}` — reload config from disk for a market

---

## 7. Project Structure

```
pimarketmaker/
├── pimm.md                     # Project specification
├── web-dashboard.md            # Web dashboard layout & UI reference
├── PLAN.md                     # This file
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

---

## 8. External Dependencies

| Package | Function | Description |
|---------|----------|-------------|
| **desktool** | `get_lot_size()` | Returns lot size dict for universe RICs |
| **desktool** | `get_trading_day_type()` | Returns `0` / `0.5` / `1` per market |
| **desktool** | `get_delta_beta_info()` | Returns single-line string with delta/beta info (polled periodically) |
| **desktool** | feed thread objects | Thread objects for risk appetite, live price, fills (shared) and inventory (per-market) |
| **desktool** | quote injection | Send dispatch DataFrame to KDB+ (to be wired later) |
| **alpha project** | queue push | External project pushes alpha DataFrames into per-market queue |
| **fastapi** | web server | HTTP + WebSocket server for dashboard |
| **uvicorn** | ASGI server | Runs FastAPI in the asyncio loop |
| **win32com** | Outlook COM | Sends access link email on startup |

---

## 9. Key Data Types

### EngineSnapshot (pushed to web UI via WebSocket)

```
markets: dict[str, pd.DataFrame]   # market_name -> DataFrame copy
scaling: dict[str, tuple]           # market_name -> (buy_scaling, sell_scaling)
recent_fills: list[TradeFill]       # recent fills across all markets
session_status: dict[str, bool]     # market_name -> active flag
session_countdowns: dict[str, float | None]
feed_status: dict[str, str]
delta_beta_info: str                # latest delta/beta info string
console_log: list[dict]            # recent log entries [{level, msg}], INFO+ captured
timestamp: datetime
```

### TradeFill

```
ric: str
side: Side (BUY / SELL)
fill_qty: float
fill_price: float
timestamp: datetime
```

---

## 10. Simulator

Standalone E2E test harness that replaces live feeds with randomized data. Simulates HK, CN, and TW markets.

**Run:** `uv run python -m pimm.simulator configs/simulator.toml [--seed N] [--port PORT]`

| Thread | Interval | Data |
|--------|----------|------|
| Risk appetite | 3s | All RICs across markets, random qty, fx_rate per market |
| Live price | 2s | All RICs, ±2% jitter from stub base prices |
| Inventory | 5s | All RICs, random 0–20000 |
| Alpha | 20s | Per-market RICs, random [-0.3, 0.3] |
| Fills | 2–6s | Trades against dispatched quotes (lot-aligned, price with slippage) |

**Fill simulator:** Realistic — fills only occur when quotes are dispatched. The dispatch callback populates a shared `live_quotes` dict; the fill simulator picks random RICs from this dict, consumes qty (up to half available), and applies ±0.2% price slippage. Cancel-all clears the dict → fills stop.

**Stub data:** Base prices, FX rates (HKD/CNY/TWD→USD), and lot sizes for 15 stocks across 3 markets.

**Config file:** `configs/simulator.toml` — separate config with sessions 00:00–23:59, notional caps $500k, full_batch_interval_m 2min, dispatch_cooldown 5s. No runtime config mutations in Python.

**Price types:** Buy side uses only `best_bid`, sell side uses only `best_ask`.

---

## 11. Implementation Tasks

### Phase 1: Infrastructure (v0.3.0 — DONE)
- [x] Config migration (TOML → configparser), config.cfg (reverted to TOML in v0.6.0)
- [x] Multi-market engine (shared loop, per-market state)
- [x] Feed adapter redesign (desktool thread objects)
- [x] File reorganization (types → quotetypes, remove lots.py, move config.py)
- [x] Universe: all RICs included, quote_status + remark columns
- [x] 91 tests, 0 lint errors

### Phase 2: Per-Country Feed Start/Stop (v0.4.0 — DONE)
- [x] Update `pimm/feeds/inventory.py` — one instance, per-country start/stop
- [x] Update `pimm/feeds/alpha.py` — one instance, per-country start/stop
- [x] Update engine loop — `start_market()` / `stop_market()` on country enable/disable
- [x] Update simulator to use per-country inventory + alpha
- [x] Update tests

### Phase 3: Web Dashboard (v0.4.0 — DONE)
- [x] Create `pimm/web/` package (server.py, email.py, static/index.html)
- [x] FastAPI app with token auth, WebSocket, snapshot serialization, command handling
- [x] Single-file dark theme dashboard (HTML + CSS + JS)
- [x] Layout: Control + Summary (top) | Delta/Beta + Log (mid) | Quoting + Trades tabs (bot)
- [x] Draggable column reordering (persisted to localStorage)
- [x] Resizable columns (quoting table + trade summary)
- [x] Resizable panels (flex-ratio based, adapts to browser resize)
- [x] Country dropdown filter + RIC regex filter
- [x] RIC sorting by region suffix then alphabetically
- [x] Summary table: Scaling / Notional / PnL per market + total
- [x] Trade fills list + trade summary table (buy/sell notional, PnL, ratio)
- [x] View Params modal, Reload Params (preserves session override + active status)
- [x] `[web]` config section (port, recipients, delta_beta_interval) → renamed to `[pimm]` in v0.6.0
- [x] Full layout & UI details documented in `web-dashboard.md`

### Phase 4: Remove PyQt6 (v0.4.0 — DONE)
- [x] Delete `pimm/gui/` folder entirely
- [x] Remove PyQt6 and pyqtdarktheme from pyproject.toml dependencies
- [x] Replace multiprocessing + mp.Queue with WebSocket snapshot push

### Phase 5: Engine Integration (v0.4.0 — DONE)
- [x] Wire FastAPI server into main.py and simulator.py (same asyncio loop via uvicorn)
- [x] Add periodic delta/beta query task
- [x] Add CRITICAL+ log handler for WebSocket
- [x] Wire WebSocket commands to engine
- [x] Update EngineSnapshot (market_configs, delta_beta_info, console_log)
- [x] Realistic fill simulator (trades against dispatched quotes, lot-aligned, slippage)
- [x] Multi-market simulation (HK, CN, TW with stub prices, fx rates, lot sizes)

### Phase 6: Naming & Cleanup (v0.4.0 — DONE)
- [x] Rename offer → ask throughout (PriceType, column labels, state names)
- [x] Remove mid price type (PriceType enum: BEST_BID and BEST_ASK only)
- [x] Buy side uses only best_bid, sell side uses only best_ask
- [x] Last price display: 2 decimal places

### Phase 7: Config & Code Quality (v0.4.0 — DONE)
- [x] Add `alpha_enabled` config switch per country (default False)
- [x] Skip alpha feed subscription when `alpha_enabled = false`
- [x] Handle `alpha_enabled` transitions on config reload (start/stop feed, zero alpha)
- [x] Remove `timezone` from config (project default: HKT)
- [x] Trade Fills / Trade Summary split changed to 1:2 ratio with resize handle
- [x] Compact all Python source to PEP 8 style (max 79 chars, grouped args)
- [x] Convert all `%` string formatting to f-strings throughout codebase
- [x] Add coding style rules to workspace `CLAUDE.md` (compact code, f-strings)

---

## 12. Current Status

### Completed (v0.2.0)
- [x] DataFrame-centric architecture (single universe DataFrame per market)
- [x] Vectorized 4-step sizing pipeline
- [x] Two-mode dispatch (full batch + partial update)
- [x] Refill logic (accumulation, threshold, cap)
- [x] Feed adapter framework (queue-based)
- [x] Mark-to-market PnL (per-stock + aggregate)
- [x] Session management (start/end/cancel-all)
- [x] GUI dashboard (single market)
- [x] Simulator harness (single market)
- [x] 86 tests, 0 lint errors

### Completed (v0.3.0)
- [x] Config migration (TOML → configparser)
- [x] Multi-market engine (shared loop, per-market state)
- [x] Feed adapter redesign (desktool thread objects)
- [x] GUI as primary control interface (PyQt6)
- [x] GUI layout redesign (controls + summary + table + fills)
- [x] File reorganization (types → quotetypes, remove lots.py, move config.py)
- [x] Universe: all RICs included, quote_status + remark
- [x] 91 tests, 0 lint errors

### Completed (v0.4.0)
- [x] Per-country feed start/stop (inventory + alpha: one instance each, manages all countries)
- [x] Replace PyQt6 GUI with web dashboard (FastAPI + WebSocket + vanilla HTML/JS)
- [x] Token-based access via email (win32com/Outlook)
- [x] Three-section layout: Control+Summary | Delta/Beta+Log | Quoting+Trades tabs
- [x] Draggable column reorder, resizable columns and panels
- [x] Country + RIC regex filters, RIC sorting by region then alphabetically
- [x] Summary table (scaling, notional, PnL per market + total)
- [x] Trade fills list + trade summary table (notional, PnL, ratio, 1:2 split)
- [x] Realistic fill simulator (trades against dispatched quotes)
- [x] Multi-market simulation (HK, CN, TW)
- [x] Rename offer → ask, remove mid price type
- [x] Remove PyQt6 and pyqtdarktheme dependencies
- [x] `alpha_enabled` config switch per country (default False, controls feed subscription)
- [x] Remove `timezone` from config (project default: HKT)
- [x] Compact PEP 8 coding style + f-strings throughout codebase

### Completed (v0.5.0)
- [x] `StateManager.update_*()` vectorized with self-filtering, returns `bool`
- [x] `FeedAdapter` base: `ric_list` param for subscription scope
- [x] `InventoryFeed` / `AlphaFeed` simplified to thin `FeedAdapter` subclasses (one per market)
- [x] Removed `register_market` / `start_market` / `stop_market` / `set_loop` from feeds
- [x] Engine: removed `_ric_to_market`, routing methods; broadcasts to all markets
- [x] Engine: per-market feeds as dicts (`inventory_feeds["HK"]`, `alpha_feeds["HK"]`)
- [x] `MarketState`: `day_type` (0/0.5/1) and `override_window` fields
- [x] Session monitor: auto-activate per `day_type` rules; user override (single window)
- [x] Web UI: Start button JS confirm for non-trading / half-day past first window
- [x] `EngineSnapshot`: `day_types` dict for frontend warning logic
- [x] Simulator + main: `_get_trading_day_type()` stub → 1
- [x] 102 tests, 0 lint errors

### Phase 8: Feed & State Redesign (v0.5.0 — DONE)
- [x] `StateManager.update_*()` — vectorized self-filtering, return `bool`
- [x] `FeedAdapter` base class — add `ric_list` param for subscription scope
- [x] `InventoryFeed` / `AlphaFeed` — simplify to thin subclasses (one instance per market)
- [x] Remove `register_market` / `start_market` / `stop_market` / `set_loop` from feeds
- [x] Engine: remove `_ric_to_market`, `_route_shared_feed`, `_route_alpha`, `_get_affected_markets`
- [x] Engine: broadcast full DataFrame to all markets, dispatch those with `update_*() == True`
- [x] Engine: store per-market feeds as dicts (`inventory_feeds["HK"]`, `alpha_feeds["HK"]`)
- [x] `MarketState`: add `day_type` (0/0.5/1) and `override_window` fields
- [x] Session monitor: auto-activate based on `day_type` rules
- [x] Session monitor: user override sets `override_window` to current/next window (single window only)
- [x] Web UI: Start button shows JS confirm warning for `day_type == 0` or `day_type == 0.5` past first window
- [x] `EngineSnapshot`: include `day_type` per market for frontend warning logic
- [x] Simulator: stub `get_trading_day_type` → always returns `1`
- [x] `main.py`: call `desktool.get_trading_day_type(market)` at init (stubbed to `1` until wired)
- [x] Update tests for new feed/state interfaces

### Phase 9: Config Restructure (v0.6.0 — DONE)
- [x] Migrate from configparser (`.cfg`) to `tomllib` (`.toml`)
- [x] Rename `WebConfig` → `PimmConfig` with general settings
- [x] Move `max_staleness`, `full_batch_interval`, `min_dispatch_interval` from market to `PimmConfig`
- [x] Add `[market_defaults]` section — shared market defaults, per-market overrides win
- [x] Rename time fields: `_m` suffix for minutes, `_s` suffix for seconds
- [x] Rename `port` → `web_port`
- [x] Create `configs/config.toml` replacing `configs/config.cfg`
- [x] Update `pimm/config.py` — `tomllib` loader, `PimmConfig`, defaults merge
- [x] Update all consumers: engine, simulator, main, web server, tests
- [x] Update test_config.py for new TOML format

### Phase 10: Engine & Config Cleanup (v0.6.0 — DONE)
- [x] TradingEngine takes `config_path` instead of `pimm_config` — loads PimmConfig internally
- [x] Create `configs/simulator.toml` — separate config for simulator (fast batches, all-day sessions, $500k notional)
- [x] Remove runtime config mutations from simulator.py
- [x] Remove `reload_market_config()` — was identical wrapper around `load_market_config()`
- [x] Rename `mname` → `mkt` in all loop variables (main.py, simulator.py, server.py)
- [x] Disable ruff I001 — allows compact multi-import lines
- [x] `StateManager(ric_list, config)` — removed `lot_sizes` param, queries desktool internally (stubbed)
- [x] Vectorized StateManager init (`index.map`, `notna`, `loc`) — no per-RIC loop
- [x] StateManager logs universe summary + missing lot sizes internally
- [x] Alpha feeds take all RICs (not just quotable) — `update_alpha()` self-filters
- [x] `pimm/utils/network.py` — `get_host_ip()` for LAN-accessible dashboard URL
- [x] Fix: stop per-market feeds when quoting disabled via Stop button
- [x] Feed start/stop logging per market in `_start_market_feeds` / `_stop_market_feeds`
- [x] Move feed push logging from sim threads to `FeedAdapter._push()` — only logs when data actually forwarded
- [x] Simulator per-market feeds no longer started eagerly at init — managed by session monitor
- [x] Fixed session column width in web dashboard for consistent button alignment
- [x] 106 tests, 0 lint errors
- [x] `FeedAdapter.market_name` param (default "ALL") — per-market feeds log `[HK]`, shared feeds log `[ALL]`
- [x] Web dashboard log panel: `WebLogHandler` captures INFO+ as `{level, msg}` (buffer 500, was CRITICAL-only)
- [x] Log panel filters: level dropdown (DEBUG→CRITICAL) + regex text filter, right-aligned in header
- [x] Log entries color-coded by level (gray/white/yellow/red+bold)
- [x] Delta and Beta panel: regex text filter (right-aligned in header, filters lines)
- [x] Mid panels use flex layout with internal scroll — no outer scrollbar overflow
- [x] In-memory log buffer increased to 2000 entries
- [x] File logging: `main.py` → `logs/pimm_{YYYYMMDD}.log`, `simulator.py` → `logs/sim_{YYYYMMDD_HHMMSS}.log` (DEBUG level)
- [x] WebSocket logs client IP on connect, disconnect, and commands (e.g. `Command 'start' for [HK] from 192.168.1.50`)

### Future
- [ ] Wire desktool real subscriptions
- [ ] Wire alpha project real subscriptions
- [ ] Wire HeartbeatMonitor.record_update() from feeds
- [ ] Wire KDB+ quote injection via dispatch callback
