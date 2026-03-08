# PLAN.md — pimm (Python Intelligent Market Maker)

## 1. Project Overview

A stock dark pool market-making engine with dynamic pricing and a real-time GUI dashboard.

**Core workflow:**
1. Load market-specific TOML config; load stock universe from CSV
2. Query lot sizes from desktool for the universe RICs
3. Initialize a universe DataFrame — the single source of truth for all per-stock state
4. Subscribe to 4 live feeds: risk appetite, inventory, trade fills, alpha signals
5. Compute quote sizes through a vectorized 4-step sizing pipeline
6. Dispatch quotes in two modes: scheduled full batch and reactive partial update
7. Monitor everything via a separate-process PyQt6 dashboard

---

## 2. How the Market Maker Works

### 2.1 Startup Sequence

```
1. Load TOML config (sessions, caps, notional limits, timing)
2. Load stock universe from CSV → list of RICs to quote
3. Query lot sizes from desktool for universe RICs
   - RICs with no lot size → log warning, skip (not quoted)
4. Initialize universe DataFrame (one row per valid RIC)
   - Static columns set once: ric, lot_size, cap (from config overrides)
   - Dynamic columns initialized to defaults: buy_raw=0, sell_raw=0,
     alpha=0, inventory=0, last_price=0, fx_rate=1, etc.
5. Start 5 feed subscription threads (risk appetite, live price, inventory, fills, alpha)
6. Start heartbeat monitor thread
7. Optionally start GUI in separate process
8. Start async engine loop
```

### 2.2 Universe DataFrame

All per-stock state lives in a single pandas DataFrame, indexed by `ric`. This is the only mutable state for the quoting engine.

**Columns:**

| Column | Source | Updated by | Description |
|--------|--------|------------|-------------|
| `ric` | CSV | startup | Stock identifier (index) |
| `lot_size` | desktool | startup | Minimum tradeable lot |
| `stock_limit` | TOML config | startup | Single-name limit (per-stock override or market default) |
| `buy_state` | risk appetite feed | `update_risk_appetite()` | Price type for buy side (e.g. BEST_BID) |
| `sell_state` | risk appetite feed | `update_risk_appetite()` | Price type for sell side |
| `buy_raw` | risk appetite feed | `update_risk_appetite()` | Raw buy quantity from KDB+ |
| `sell_raw` | risk appetite feed | `update_risk_appetite()` | Raw sell quantity from KDB+ |
| `last_price` | live price feed (KDB+ tick) | `update_live_price()` | Stock price in local currency |
| `fx_rate` | risk appetite feed | `update_risk_appetite()` | Local currency → USD |
| `alpha` | alpha feed | `update_alpha()` | Alpha signal ∈ [-1, 1] |
| `inventory` | inventory feed | `update_inventory()` | Current position |
| `live_buy_qty` | dispatch | `_dispatch_*()` | Currently dispatched buy quantity |
| `live_sell_qty` | dispatch | `_dispatch_*()` | Currently dispatched sell quantity |
| `last_sent_time` | dispatch | `_dispatch_*()` | Timestamp of last dispatch for this stock |
| `filled_buy_since_dispatch` | fill event | `process_fill()` | Cumulative buy fills since last full batch (reset on full batch) |
| `filled_sell_since_dispatch` | fill event | `process_fill()` | Cumulative sell fills since last full batch (reset on full batch) |
| `pnl_buy_qty` | fill event | `_handle_fills()` | Cumulative bought qty today (reset on session start) |
| `pnl_buy_cost` | fill event | `_handle_fills()` | Cumulative buy cost today (sum of fill_price * fill_qty) |
| `pnl_sell_qty` | fill event | `_handle_fills()` | Cumulative sold qty today (reset on session start) |
| `pnl_sell_revenue` | fill event | `_handle_fills()` | Cumulative sell revenue today (sum of fill_price * fill_qty) |

Feed updates merge into the DataFrame by RIC (left join — only universe RICs are kept, unknown RICs are ignored).

**Thread safety:** No locks are needed on the universe DataFrame. Feed threads never touch it directly — they push DataFrames into the `asyncio.Queue` via `loop.call_soon_threadsafe()`. Only the engine's async loop (single thread) reads and writes the universe DataFrame. The GUI process receives a **copy** via `mp.Queue`, so there is no concurrent access.

### 2.3 Steady-State Event Loop

```
Feed event arrives (risk appetite / live price / inventory / alpha / fill)
  │
  ├─ Merge feed data into universe DataFrame
  │   - Risk appetite: update buy_raw, sell_raw, buy_state, sell_state, fx_rate
  │   - Live price: update last_price
  │   - Inventory: update inventory column
  │   - Alpha: update alpha column (clamped to [-1, 1])
  │   - Fill: accumulate into filled_buy/sell_since_dispatch + PnL accumulators
  │
  ├─ If session is active → run dispatch decision logic
  │
  └─ Push GUI snapshot (copy of DataFrame, non-blocking)
```

### 2.4 Dispatch Decision Logic

```
_try_dispatch():
  │
  ├─ 1. COOLDOWN CHECK
  │     Was last dispatch (full or partial) < min_dispatch_interval ago?
  │     → YES: do nothing, wait for next event
  │
  ├─ 2. FULL BATCH CHECK
  │     Has it been >= full_batch_interval since last full batch?
  │     (Or is this the very first dispatch?)
  │     → YES: run full batch
  │
  ├─ 3. COMPUTE PARTIAL (vectorized)
  │     Run 4-step pipeline on full DataFrame (using last batch's scaling)
  │     Apply inventory constraint (post-pipeline)
  │     Filter to stocks needing update:
  │       Case A — Quote change: |optimal - live| / live > partial_change_threshold
  │       Case B — Refill: filled >= refill_fill_threshold * live_qty
  │     Cap refill stocks: dispatch qty ≤ optimal - filled_since_last_full_dispatch
  │     → Empty: do nothing
  │
  ├─ 4. NOTIONAL CHECK
  │     If applying the partial changes would push total notional over limit
  │     → YES: promote to full batch (recalculates scaling)
  │
  └─ 5. SEND PARTIAL
        Dispatch only the selected stocks
```

### 2.5 The 4-Step Sizing Pipeline (Vectorized)

The pipeline computes the "optimal quote" — what we'd want to trade based on current inputs. Inventory constraint is applied separately at dispatch time, not in the pipeline.

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

For **full batch**: compute fresh scaling factors in step 3.
For **partial update**: substitute cached `buy_scaling` / `sell_scaling` from last full batch.

### 2.6 Pre-Dispatch: Inventory Constraint

Applied after the sizing pipeline, right before dispatch:

```python
df['sell_dispatch'] = df['sell_optimal'].clip(upper=df['inventory'].clip(lower=0))
df['buy_dispatch']  = df['buy_optimal']  # buy side unaffected by inventory
```

This keeps the stored optimal quote unconstrained by inventory (inventory changes frequently and shouldn't affect the optimal calculation).

### 2.7 Full Batch vs Partial Update

**Full Batch** (scheduled, every `full_batch_interval` minutes):
- Runs vectorized 4-step pipeline on entire DataFrame
- Recalculates global scaling factors from scratch
- Applies inventory constraint
- Dispatches all stocks with non-zero quantities
- Updates `live_buy_qty`, `live_sell_qty`, `last_sent_time` for all stocks
- Resets `filled_buy/sell_since_dispatch` to 0 for all stocks
- Saves scaling factors for partial updates to reuse

**Partial Update** (reactive, between full batches):
- Runs vectorized 4-step pipeline with cached scaling factors
- Applies inventory constraint
- Selects stocks to dispatch (two cases):
  - **Quote change:** `|optimal - live| / live > partial_change_threshold` (e.g. 10%)
  - **Refill:** `filled_since_dispatch >= refill_fill_threshold * live_qty` (e.g. 50%)
- For refill stocks: dispatch qty is capped at `optimal - filled_since_last_full_dispatch`
  (ensures total execution across multiple refills does not exceed the optimal amount)
- If expected total notional would breach limit → auto-promote to full batch
- Otherwise dispatches only the selected stocks

### 2.8 Refill Logic

Refill is one case of partial update, triggered by fills exceeding a threshold.

**On fill event:**
- Accumulate `fill_qty` into `filled_buy/sell_since_dispatch` (do NOT reduce `live_buy/sell_qty` — the dark pool tracks remaining quantity internally)

**On partial update (refill path):**
- Trigger condition: `filled_since_dispatch >= refill_fill_threshold * live_qty`
  (e.g. 50% of the live quote has been filled)
- Compute dispatch quantity normally through the 4-step pipeline
- Cap: `dispatch_qty = min(optimal_qty, optimal_qty - filled_since_last_full_dispatch)`
  (prevents total execution from exceeding optimal across multiple refills)
- `filled_since_dispatch` is **not** reset on partial update — it accumulates across the full batch cycle and is only reset on full batch

**Example:**
```
Live quote: buy 1000
Fill: 600 (60% > 50% threshold → trigger refill)
Optimal: 1200
Refill qty: min(1200, 1200 - 600) = 600
→ Send 600

Another fill: 300 (total filled = 900)
900 > 50% of 600 (live) → trigger refill
Refill qty: min(1200, 1200 - 900) = 300
→ Send 300

Total execution: 900 + 300 = 1200 = optimal ✓
```

### 2.9 Session Management

- **Session windows:** Configured as `"HH:MM-HH:MM"` (start inclusive, end exclusive)
- **Session start:** Immediate full batch dispatch
- **Session end:** Cancel-all batch (zero-qty for all stocks)
- **Between sessions:** No quoting, no dispatching
- **Order validity:** Orders valid for `order_valid_time` minutes; `refresh_buffer` seconds for proactive refresh

### 2.10 Output

Dispatched quotes are a slice of the universe DataFrame sent to the KDB+ injector:

```
ric, buy_state, buy_qty, sell_state, sell_qty
```

---

## 3. Stock Universe

### 3.1 CSV Config File

The list of RICs to quote is loaded from a CSV file, referenced in the TOML config:

```toml
[market]
universe_file = "configs/hk_universe.csv"
```

CSV format (single column with header):
```csv
ric
0005.HK
0700.HK
9988.HK
1299.HK
0388.HK
```

### 3.2 Startup Flow

```
1. Load TOML config → get universe_file path
2. Read CSV → list of RICs
3. Query desktool for lot sizes for those RICs
4. For each RIC:
   - Lot size found → include in universe DataFrame
   - Lot size NOT found → log warning, skip (not quoted)
5. Only RICs with valid lot sizes participate in quoting
```

---

## 4. Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.11+ |
| Concurrency | `asyncio` (engine) + `threading` (feeds) + `multiprocessing` (GUI) |
| Config | Per-market `.toml` via `tomllib` + universe `.csv` |
| GUI | PyQt6 + `pyqtdarktheme` |
| Alerting | `winsound.Beep` on feed staleness |
| Package mgmt | `uv` |
| Linting | `ruff` |
| Testing | `pytest` |

---

## 5. Project Structure

```
pimarketmaker/
├── spec.md                     # Original product specification
├── PLAN.md                     # This file
├── pyproject.toml              # Build config, deps, tool settings
├── configs/
│   ├── __init__.py
│   ├── config.py               # TOML config loader + universe CSV loader
│   ├── config.toml             # Market config ([HK] section)
│   └── hk_universe.csv         # HK stock universe (RICs to quote)
├── pimm/
│   ├── __init__.py
│   ├── types.py                # Shared data types (enums, TradeFill, EngineSnapshot)
│   ├── main.py                 # Production entry point
│   ├── simulator.py            # E2E simulation harness
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── state.py            # Universe DataFrame management (feed updates)
│   │   ├── sizing.py           # 4-step sizing pipeline (vectorized pandas)
│   │   ├── dispatcher.py       # Full batch + partial update builder
│   │   ├── loop.py             # Async trading engine + session monitor
│   │   └── refill.py           # Fill accumulation + refill trigger logic
│   ├── feeds/
│   │   ├── __init__.py
│   │   ├── base.py             # FeedAdapter base (thread → asyncio bridge)
│   │   ├── risk_appetite.py    # Risk appetite feed (desktool stub)
│   │   ├── live_price.py       # Live price feed (KDB+ tick stub)
│   │   ├── inventory.py        # Inventory feed (desktool stub)
│   │   ├── fills.py            # Trade fills feed (desktool stub)
│   │   ├── alpha.py            # Alpha signal feed (alphaflow stub)
│   │   └── heartbeat.py        # Feed staleness monitor
│   ├── gui/
│   │   ├── __init__.py
│   │   ├── process.py          # GUI process bootstrap (multiprocessing)
│   │   ├── dashboard.py        # Main PyQt6 dashboard window
│   │   └── widgets.py          # Custom widgets (ScalingBanner, AlphaItem, etc.)
│   └── utils/
│       ├── __init__.py
│       ├── lots.py             # Lot size table builder
│       └── time.py             # HKT timezone helpers + session checks
└── test/
    ├── __init__.py
    ├── conftest.py             # Shared fixtures
    ├── test_config.py          # Config loading + universe CSV
    ├── test_sizing.py          # Vectorized sizing pipeline
    ├── test_fills.py           # Fill accumulation + state updates
    ├── test_refill.py          # Refill trigger + capping logic
    └── test_session.py         # Session windows + refresh timing
```

---

## 6. Feed Interfaces

All feeds push `pd.DataFrame` via threaded callbacks into the engine's async queue.

| Feed | Source | Columns | Frequency |
|------|--------|---------|-----------|
| Risk Appetite | desktool (KDB+) | `ric, buy_state, buy_qty, sell_state, sell_qty, fx_rate` | Periodic (global snapshot) |
| Live Price | KDB+ (tick) | `ric, last_price` | Real-time (on tick) |
| Inventory | desktool (KDB+) | `ric, inventory` | On change |
| Trade Fills | desktool (KDB+) | `ric, side, fill_qty, fill_price, timestamp` | On fill |
| Alpha | alphaflow | `ric, alpha` (float in [-1, 1]) | On change |

**Note:** `last_price` is the stock price in local currency, received via a dedicated real-time KDB+ tick subscription. `fx_rate` converts local currency → USD.

---

## 7. Configuration

### 7.1 TOML Structure (`configs/config.toml`)

The market name is the TOML section name. All settings for that market live under it — no separate `[market_settings]` section.

```toml
[HK]
timezone = "Asia/Hong_Kong"
universe_file = "configs/hk_universe.csv"
sessions = ["09:30-12:00", "13:00-16:00"]
order_valid_time = 5          # minutes
refresh_buffer = 15           # seconds
full_batch_interval = 10      # minutes between full batch updates
min_dispatch_interval = 5     # seconds between any two dispatches
single_name_cap = 50000
max_buy_notional = 10000000   # USD
max_sell_notional = 10000000  # USD
max_staleness = 30            # seconds
partial_change_threshold = 0.10   # 10% change required for partial update
refill_fill_threshold = 0.50      # 50% filled to trigger refill

[overrides.stock_limit.HK]
"0005.HK" = 100000      # HSBC gets higher limit
"0700.HK" = 20000       # Tencent gets lower limit
```

### 7.2 Universe CSV (`configs/hk_universe.csv`)

```csv
ric
0005.HK
0700.HK
9988.HK
1299.HK
0388.HK
```

### 7.3 Hierarchical Override Resolution

`MarketConfig.get_stock_limit(ric)` checks `[overrides.stock_limit.{market}]` first, falls back to the market's `single_name_cap` default.

---

## 8. GUI Dashboard

Runs in a separate `multiprocessing.Process` to ensure zero impact on trading latency.

### 8.1 Data Flow

Engine → `mp.Queue` → `EngineSnapshot` (contains DataFrame copy) → GUI polls every 100ms via `QTimer`

### 8.2 Layout

| Area | Widget | Content |
|------|--------|---------|
| Top banner | `ScalingBanner` | Global buy/sell scaling factors. Orange when < 1.0 |
| Main table | `QTableWidget` | RIC, Bid State, Bid Qty, Offer State, Offer Qty, Last Price, Inventory, Alpha, PnL, Filled Since Dispatch |
| Alpha cells | `AlphaItem` | Color-coded: green (> 0.05), red (< -0.05) |
| Right panel | `PnlPanel` | Mark-to-market PnL (Local + USD aggregate) |
| Right panel | `TradeFlowLog` | Scrolling fill events |
| Status bar | `QStatusBar` | Session active/inactive, countdown, feed status |

---

## 9. Concurrency Architecture

```
Main Process
├── asyncio event loop (main thread)
│   └── TradingEngine.run()
│       └── asyncio.Queue ← (event_type, DataFrame)
│
├── Thread: feed-risk_appetite   (daemon) ─┐
├── Thread: feed-live_price      (daemon) ─┤ All push via
├── Thread: feed-inventory       (daemon) ─┤ loop.call_soon_threadsafe()
├── Thread: feed-fills           (daemon) ─┤
├── Thread: feed-alpha           (daemon) ─┘
├── Thread: heartbeat-monitor    (daemon)
│
└── GUI Process (multiprocessing, daemon)
    └── mp.Queue ← EngineSnapshot (with DataFrame copy)
```

---

## 10. External Dependencies (stubs)

| Package | Usage | Status |
|---------|-------|--------|
| **desktool** | Risk appetite, inventory, fills subscriptions + lot sizes + quote injection to KDB+ | Stub — `_subscribe()` is a no-op |
| **alphaflow** | Alpha signal subscription | Stub — `_subscribe()` is a no-op |

All feed adapters have `on_update(df)` callbacks ready for wiring.

---

## 11. Simulator

Standalone E2E test harness that replaces live feeds with randomized data.

**Run:** `uv run python -m pimm.simulator configs/hk.toml [--gui] [--seed N]`

| Thread | Interval | Data |
|--------|----------|------|
| Risk appetite | 3s | All universe RICs, random qty 500–30000, `fx_rate=0.128` |
| Live price | 2s | All universe RICs, `last_price` ±2% jitter from base |
| Inventory | 5s | Random 0–20000 |
| Alpha | 20s | Random [-0.3, 0.3] |
| Fills | 4s | 1–4 random RICs, random side, qty 100–5000 |

**Config overrides:** sessions 00:00–23:59, notional caps $500k, full_batch_interval 2min, min_dispatch_interval 5s.

**Logs:** `logs/sim_<YYYYMMDD_HHMMSS>.log`

---

## 12. Key Data Types

### Universe DataFrame (central state — one row per RIC)

```
ric (index)                | str      | Stock identifier
lot_size                   | int      | Minimum lot from desktool
stock_limit                | float    | Single-name limit (from config)
buy_state                  | str      | Buy price type (e.g. "BEST_BID")
sell_state                 | str      | Sell price type
buy_raw                    | float    | Raw buy qty from risk appetite
sell_raw                   | float    | Raw sell qty from risk appetite
last_price                 | float    | Stock price in local currency
fx_rate                    | float    | Local → USD conversion rate
alpha                      | float    | Alpha signal ∈ [-1, 1]
inventory                  | float    | Current position
live_buy_qty               | float    | Currently dispatched buy qty
live_sell_qty              | float    | Currently dispatched sell qty
last_sent_time             | datetime | Last dispatch time for this stock
filled_buy_since_dispatch  | float    | Cumulative buy fills since last full batch
filled_sell_since_dispatch | float    | Cumulative sell fills since last full batch
pnl_buy_qty                | float    | Cumulative bought qty today (reset on session start)
pnl_buy_cost               | float    | Cumulative buy cost today
pnl_sell_qty               | float    | Cumulative sold qty today (reset on session start)
pnl_sell_revenue           | float    | Cumulative sell revenue today
```

### EngineSnapshot (pushed to GUI via mp.Queue)

```python
universe: pd.DataFrame          # copy of universe DataFrame
buy_scaling: float
sell_scaling: float
recent_fills: list[TradeFill]
session_active: bool
session_end_countdown: float | None
feed_status: dict[str, str]
timestamp: datetime
```

### TradeFill

```python
ric: str
side: Side                      # BUY / SELL
fill_qty: float
fill_price: float
timestamp: datetime
```

---

## 13. Test Coverage

**68 tests, 0 lint errors**

| File | Tests | Covers |
|------|-------|--------|
| `test_config.py` | 6 | Config loading, session parsing, defaults, overrides, notional limits |
| `test_sizing.py` | 22 | Alpha skew, single-name cap, notional scaling, inventory constraint, lot rounding, full pipeline |
| `test_fills.py` | 9 | Fill aggregation, VWAP, state feed updates, live price updates, unknown RIC handling |
| `test_refill.py` | 9 | Aggregate fills, process_fill, freshness guard eligibility, mark_dispatched |
| `test_session.py` | 9 | Session windows, boundaries, countdown, refresh timing |
| `test_pnl.py` | 10 | PnL accumulation, formula (buy/sell/mixed), price recomputation, daily reset, aggregate (local + USD) |

---

## 14. Current Status

### Completed (v0.2.0 — Full Rewrite, Mar 2026)

- [x] Full rewrite to flat layout (`pimm/` at root, no `src/`)
- [x] No type hints, inline comments, per python-CLAUDE.md conventions
- [x] New TOML config structure (`[HK]` sections, `configs/config.toml`)
- [x] CSV-based stock universe (`configs/hk_universe.csv`)
- [x] DataFrame-centric architecture (universe DataFrame replaces StockState)
- [x] Vectorized 4-step sizing pipeline (pandas vector ops, no per-stock loops)
- [x] Inventory constraint at dispatch time (post-pipeline)
- [x] Partial change threshold (`partial_change_threshold`, 10%)
- [x] New refill logic (fill accumulation, threshold trigger, optimal cap)
- [x] Fill counters reset only on full batch
- [x] Two-mode dispatch (full batch + partial update)
- [x] Dispatch cooldown (`min_dispatch_interval`)
- [x] Partial → full batch auto-promotion on notional breach
- [x] Feed adapter framework (thread → asyncio bridge, stubs)
- [x] Heartbeat monitor with winsound alert
- [x] Session management (start/end/cancel-all)
- [x] GUI dashboard reads from DataFrame in EngineSnapshot
- [x] ScalingBanner, AlphaItem, PnlPanel, TradeFlowLog widgets
- [x] Simulator harness with 4 sim threads + GUI support
- [x] 68 tests, 0 lint errors
- [x] Test dir: `test/` (not `tests/`)

### Remaining TODO

- [ ] Wire desktool real subscriptions into feed adapters
- [ ] Wire alphaflow real subscription
- [ ] Wire `HeartbeatMonitor.record_update()` calls from feed adapters
- [x] Mark-to-market PnL (per-stock + aggregate local/USD, daily reset on session start)
- [ ] Wire KDB+ real-time tick subscription into LivePriceFeed
- [ ] Wire `TradeFlowLog.add_dispatch_event()` from engine
- [ ] Possibly replace alpha skew formula
- [ ] Manual GUI testing with `--gui` flag
