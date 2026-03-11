# PLAN.md — pimm (Python Intelligent Market Maker)

## 1. Project Overview

A multi-market dark pool market-making engine with dynamic pricing, controlled and monitored through a real-time GUI dashboard.

**Core workflow:**
1. Launch GUI — the primary control and monitoring interface
2. Load config.cfg (all markets); per market: load universe CSV, query lot sizes from desktool
3. Check trading day type per market (desktool); auto-enable/disable accordingly
4. Initialize per-market universe DataFrame — single source of truth for all per-stock state
5. Subscribe to shared feeds (risk appetite, live price, inventory, fills) via desktool threads
6. Subscribe to per-market alpha feeds via queue (external alpha project)
7. Compute quote sizes through a vectorized 4-step sizing pipeline
8. Dispatch quotes in two modes: scheduled full batch and reactive partial update
9. GUI displays live status, trade fills, PnL; provides start/stop, param view/reload controls

---

## 2. How the Market Maker Works

### 2.1 Startup Sequence

```
1. Launch GUI process (always starts)
2. Load config.cfg → MarketConfig per section ([HK], [TW], etc.)
3. For each market:
   a. Query desktool.get_trading_day_type() → 0 (non-trading) / 0.5 (half) / 1 (full)
   b. Load universe CSV → list of RICs
   c. Query lot sizes from desktool.get_lot_size() for universe RICs
      - RICs with no lot size → set quote_status=False, remark="no lot size"
   d. Initialize universe DataFrame (one row per RIC, all included)
   e. Determine active sessions based on trading day type:
      - Full day: all configured sessions
      - Half day: first (morning) session only
      - Non-trading: disabled by default (GUI can override)
4. Start shared feed threads (risk appetite, live price, inventory, fills)
   - Desktool provides thread objects; engine controls start/stop
5. Start per-market alpha feed queue polling threads
6. Start heartbeat monitor
7. Start shared async engine loop (manages all markets)
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
| `inventory` | inventory feed | `update_inventory()` | Current position |
| `live_buy_qty` | dispatch | `_dispatch_*()` | Currently dispatched buy quantity |
| `live_sell_qty` | dispatch | `_dispatch_*()` | Currently dispatched sell quantity |
| `last_sent_time` | dispatch | `_dispatch_*()` | Timestamp of last dispatch |
| `filled_buy_since_dispatch` | fill event | `accumulate_fills()` | Cumulative buy fills since last full batch |
| `filled_sell_since_dispatch` | fill event | `accumulate_fills()` | Cumulative sell fills since last full batch |
| `pnl_buy_qty` | fill event | `_handle_fills()` | Cumulative bought qty today |
| `pnl_buy_cost` | fill event | `_handle_fills()` | Cumulative buy cost today |
| `pnl_sell_qty` | fill event | `_handle_fills()` | Cumulative sold qty today |
| `pnl_sell_revenue` | fill event | `_handle_fills()` | Cumulative sell revenue today |

**Thread safety:** No locks needed. Feed threads push into `asyncio.Queue` via `call_soon_threadsafe()`. Only the engine's async loop reads/writes DataFrames. GUI receives copies via `mp.Queue`.

### 2.3 Steady-State Event Loop

```
Feed event arrives (risk appetite / live price / inventory / alpha / fill)
  │
  ├─ Route to correct market by RIC membership
  │
  ├─ Merge feed data into market's universe DataFrame
  │
  ├─ If market's session is active → run dispatch decision logic for that market
  │
  └─ Push GUI snapshot (copies of all market DataFrames, non-blocking)
```

### 2.4 Dispatch Decision Logic (per market)

```
_try_dispatch(market):
  │
  ├─ 1. COOLDOWN CHECK
  │     Was last dispatch < min_dispatch_interval ago?
  │     → YES: do nothing
  │
  ├─ 2. FULL BATCH CHECK
  │     Has it been >= full_batch_interval since last full batch?
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

**Full Batch** (scheduled, every `full_batch_interval` minutes per market):
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
- **Half trading day:** Only morning (first) session active
- **Session start:** Immediate full batch dispatch + PnL reset
- **Session end:** Cancel-all batch (zero-qty for all stocks)
- **Order validity:** `order_valid_time` minutes with `refresh_buffer` seconds

---

## 3. Configuration

### 3.1 Config File (`configs/config.cfg`)

Uses Python `configparser`. One section per market, all in one file.

```ini
[HK]
timezone = Asia/Hong_Kong
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

[HK.overrides]
0005.HK = 100000
0700.HK = 20000

[TW]
timezone = Asia/Taipei
universe_file = configs/tw_universe.csv
sessions = 09:00-13:30
...
```

### 3.2 Config Loader (`pimm/config.py`)

- Reads `config.cfg` using `configparser`
- Returns `MarketConfig` per section
- Resolves per-stock overrides from `[{market}.overrides]`
- Supports reload at runtime (triggered by GUI button)

### 3.3 Trading Day Type

`desktool.get_trading_day_type()` returns per-market trading day type at startup:
- `1` — **Full:** All sessions active
- `0.5` — **Half:** Morning session only
- `0` — **Non-trading:** Disabled (GUI can override)

---

## 4. Feed Interfaces

### 4.1 Feed Adapter Base Class

`FeedAdapter` manages a desktool subscription thread + queue polling.

**Init parameters:**
- `event_type` — event name for engine routing
- `engine_push` — callback to push (event_type, DataFrame) to engine
- `thread` — desktool thread object (optional, None for alpha)
- `data_queue` — `queue.Queue` for receiving DataFrames
- `service_name` — KDB+ service name
- `table_name` — KDB+ table name
- `recovery_query` / `recovery_params` — initial state recovery
- `filter_query` / `filter_params` — real-time filter

**Start:** starts the desktool thread (if provided) + starts queue polling loop.
**Stop:** stops the desktool thread + stops polling.
**Simulator:** calls `feed.on_update(df)` directly (bypasses queue).

### 4.2 Feed Table

| Feed | Source | Shared | Thread | Columns |
|------|--------|--------|--------|---------|
| Risk Appetite | desktool (KDB+) | Yes | desktool | `ric, buy_state, buy_qty, sell_state, sell_qty, fx_rate` |
| Live Price | desktool (KDB+) | Yes | desktool | `ric, last_price` |
| Inventory | desktool (KDB+) | Yes | desktool | `ric, inventory` |
| Trade Fills | desktool (KDB+) | Yes | desktool | `ric, side, fill_qty, fill_price, timestamp` |
| Alpha | external project | Per-market | None | `ric, alpha` (float in [-1, 1]) |

Shared feeds serve all markets; events routed by RIC membership.

---

## 5. GUI Dashboard

### 5.1 Data Flow

- Engine → `mp.Queue` → `EngineSnapshot` (per-market DataFrame copies + summary) → GUI polls via `QTimer`
- GUI → `mp.Queue` → commands (start/stop, reload config) → engine processes

### 5.2 Layout

**Top Left — Country Control Panel (static, all countries visible):**
- Per country row: market name, trading day type, session window, start/stop button
- Per country: "View Params" button (read-only popup), "Reload Params" button (re-reads config.cfg)

**Top Right — Global Summary:**
- Per-market scaling factors (buy/sell)
- Total notional by side (USD)
- Aggregate PnL (local + USD)

**Middle — Quoting Table (with country filter):**
- Combined optimal quotes + current live quoting info per stock
- Country filter dropdown to narrow view
- Columns: RIC, Bid State, Bid Qty, Offer State, Offer Qty, Last Price, Inventory, Alpha, PnL, Filled Since Dispatch

**Bottom — Trade Fills (filtered by country filter above):**
- Scrolling list of recent fill events
- Filtered by same country selection

**Theme:** Dark mode (`pyqtdarktheme`)

---

## 6. Architecture

### 6.1 Concurrency

```
Main Process
├── asyncio event loop (main thread)
│   └── TradingEngine.run()
│       ├── Per-market state (DataFrame, dispatch timing, session)
│       └── asyncio.Queue ← (event_type, market, DataFrame)
│
├── Thread: feed-risk_appetite  (shared, desktool thread) ─┐
├── Thread: feed-live_price     (shared, desktool thread) ─┤ All push via
├── Thread: feed-inventory      (shared, desktool thread) ─┤ loop.call_soon_threadsafe()
├── Thread: feed-fills          (shared, desktool thread) ─┘
├── Thread: feed-alpha-HK      (per-market, queue polling) ─┐
├── Thread: feed-alpha-TW      (per-market, queue polling) ─┤
├── Thread: heartbeat-monitor   (daemon)
│
└── GUI Process (multiprocessing)
    ├── mp.Queue ← EngineSnapshot (read)
    └── mp.Queue → commands (write)
```

### 6.2 Feed Routing

Shared feeds push DataFrames containing RICs from multiple markets. Engine routes each row to the correct market's DataFrame by RIC membership.

### 6.3 GUI Commands

GUI sends commands to engine via `mp.Queue`:
- `("start", market_name)` — start quoting for a market
- `("stop", market_name)` — stop quoting for a market
- `("reload", market_name)` — reload config from disk for a market

---

## 7. Project Structure

```
pimarketmaker/
├── pimm.md                     # Project specification
├── PLAN.md                     # This file
├── pyproject.toml              # Build config, deps, tool settings
├── configs/
│   ├── config.cfg              # All markets config (configparser format)
│   ├── hk_universe.csv         # HK stock universe
│   └── tw_universe.csv         # TW stock universe (example)
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
│   │   ├── risk_appetite.py    # Risk appetite feed (desktool thread)
│   │   ├── live_price.py       # Live price feed (desktool thread)
│   │   ├── inventory.py        # Inventory feed (desktool thread)
│   │   ├── fills.py            # Trade fills feed (desktool thread)
│   │   ├── alpha.py            # Alpha signal feed (queue-only, no thread)
│   │   └── heartbeat.py        # Feed staleness monitor
│   ├── gui/
│   │   ├── __init__.py
│   │   ├── process.py          # GUI process bootstrap (multiprocessing)
│   │   ├── dashboard.py        # Main PyQt6 dashboard window
│   │   └── widgets.py          # Custom widgets (ScalingBanner, PnlPanel, etc.)
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

---

## 8. External Dependencies

| Package | Function | Description |
|---------|----------|-------------|
| **desktool** | `get_lot_size()` | Returns lot size dict for universe RICs |
| **desktool** | `get_trading_day_type()` | Returns `0` / `0.5` / `1` per market |
| **desktool** | feed thread objects | Thread objects for risk appetite, live price, inventory, fills subscriptions |
| **desktool** | quote injection | Send dispatch DataFrame to KDB+ (to be wired later) |
| **alpha project** | queue push | External project pushes alpha DataFrames into per-market queue |

---

## 9. Key Data Types

### EngineSnapshot (pushed to GUI via mp.Queue)

```
markets: dict[str, pd.DataFrame]   # market_name -> DataFrame copy
scaling: dict[str, tuple]           # market_name -> (buy_scaling, sell_scaling)
recent_fills: list[TradeFill]       # recent fills across all markets
session_status: dict[str, bool]     # market_name -> active flag
session_countdowns: dict[str, float | None]
feed_status: dict[str, str]
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

Standalone E2E test harness that replaces live feeds with randomized data.

**Run:** `uv run python -m pimm.simulator configs/config.cfg [--seed N]`

| Thread | Interval | Data |
|--------|----------|------|
| Risk appetite | 3s | All RICs across markets, random qty, fx_rate per market |
| Live price | 2s | All RICs, ±2% jitter from base |
| Inventory | 5s | Random 0–20000 |
| Alpha | 20s | Random [-0.3, 0.3] per market |
| Fills | 4s | 1–4 random RICs, random side/qty |

**Config overrides:** sessions 00:00–23:59, notional caps $500k, full_batch_interval 2min, dispatch_cooldown 5s.

---

## 11. Implementation Tasks

### Phase 1: Infrastructure Changes
- [ ] Create `configs/config.cfg` from current `config.toml` (configparser format)
- [ ] Rewrite `pimm/config.py` (moved from `configs/config.py`) to use `configparser`
  - [ ] Load all market sections from one file
  - [ ] Support `[{market}.overrides]` for per-stock limits
  - [ ] Add `reload_config()` for runtime reload
  - [ ] Add `load_all_markets()` returning dict of MarketConfig
- [ ] Move `pimm/types.py` → `pimm/utils/quotetypes.py`, update all imports
- [ ] Remove `pimm/utils/lots.py` (lot sizes from desktool dict)
- [ ] Remove `configs/__init__.py`, `configs/hk.toml`, `configs/config.toml`
- [ ] Update `pyproject.toml` if needed

### Phase 2: Multi-Market Engine
- [ ] Refactor `TradingEngine` to manage multiple markets
  - [ ] Dict of per-market `StateManager` instances
  - [ ] Per-market dispatch timing (last_full_batch, last_dispatch, cooldown)
  - [ ] Per-market session monitor (timezone, windows, active flag)
  - [ ] Feed event routing by RIC membership
- [ ] Add trading day type check (desktool.get_trading_day_type(), returns 0/0.5/1)
- [ ] Per-market session logic (full/half/non-trading day)
- [ ] GUI command queue processing (start/stop/reload per market)

### Phase 3: Feed Adapter Redesign
- [ ] Rewrite `FeedAdapter` base class
  - [ ] Accept desktool thread object (optional)
  - [ ] Accept subscription params (service_name, table_name, recovery/filter query/params)
  - [ ] Start: start desktool thread + start queue polling
  - [ ] Stop: stop thread + stop polling
  - [ ] Keep `on_update()` for simulator path
- [ ] Update concrete feed classes with subscription params
- [ ] Alpha feed: queue-only (no thread), per-market instances

### Phase 4: GUI Redesign
- [ ] New layout: top-left controls, top-right summary, middle table, bottom fills
- [ ] Country control panel (all markets visible)
  - [ ] Trading day type display
  - [ ] Session window display
  - [ ] Start/Stop button per market
  - [ ] "View Params" button (read-only popup)
  - [ ] "Reload Params" button (sends reload command to engine)
- [ ] Global summary panel (scaling, notional, PnL)
- [ ] Quoting table with country filter
  - [ ] Combined optimal + live quoting info
  - [ ] Country filter dropdown
- [ ] Trade fills panel with country filter
- [ ] GUI always starts (remove `--no-gui` / make `--gui` default)
- [ ] Wire command queue (GUI → engine)

### Phase 5: Update Tests
- [ ] Update `test_config.py` for configparser format + multi-market
- [ ] Update `test_feeds.py` for new FeedAdapter init signature
- [ ] Update `test_fills.py`, `test_pnl.py` for multi-market state
- [ ] Add tests for feed routing by RIC
- [ ] Add tests for GUI commands (start/stop/reload)
- [ ] Update `conftest.py` fixtures

### Phase 6: Simulator Update
- [ ] Update simulator for multi-market
- [ ] GUI always launches in simulator mode
- [ ] Simulate multiple markets with different RIC sets

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

### In Progress (v0.3.0 — Multi-Market + GUI Control)
- [ ] Config migration (TOML → configparser)
- [ ] Multi-market engine (shared loop, per-market state)
- [ ] Feed adapter redesign (desktool thread objects)
- [ ] GUI as primary control interface
- [ ] GUI layout redesign (controls + summary + table + fills)
- [ ] File reorganization (types → quotetypes, remove lots.py, move config.py)

### Future
- [ ] Wire desktool real subscriptions
- [ ] Wire alpha project real subscriptions
- [ ] Wire HeartbeatMonitor.record_update() from feeds
- [ ] Wire TradeFlowLog.add_dispatch_event() from engine
