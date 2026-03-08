# Project Specification: pimm (Python Intelligent Market Maker)

## 1. High-Level Overview
**Goal:** A multi-asset market-making engine for dark pools using dynamic pricing and a real-time monitoring dashboard.
**Core Workflow:**
1. **Initialization:** Load market-specific TOML config; load stock universe from CSV; query lot sizes from desktool.
2. **State:** Maintain a universe DataFrame (one row per RIC) as the single source of truth for all per-stock state.
3. **Feeds:** Subscribe to KDB+ (Risk Appetite, Inventory, Fills) and Alpha feeds. Feed updates merge into the DataFrame.
4. **Quoting:** Compute sizes via a vectorized 4-step sizing pipeline. Apply inventory constraint at dispatch time. Dispatch in two modes: scheduled full batch and reactive partial update.
5. **Monitoring:** A separate process GUI displays live status and PnL.

## 2. Technical Stack
* **Language:** Python 3.11+
* **Concurrency:** `asyncio` (Trading) + `threading` (Feed Subs) + `multiprocessing` (GUI).
* **Configuration:** One `.toml` file per market (using `tomllib`) + one `.csv` for stock universe.
* **State:** Single pandas DataFrame per market — all quoting logic uses vectorized operations.
* **GUI Framework:** `PyQt6` + `pyqtdarktheme`.
* **Alerting:** `winsound.Beep` for heartbeat failures.

## 3. Configuration & Startup Logic

### A. Hierarchical Config
Each market has a standalone `.toml` file.
* **Global Market Default:** Settings in `[market_settings]` apply to all stocks by default.
* **Overrides:** Settings in `[overrides.stocks."RIC"]` take precedence over defaults for that specific ticker.

### B. Stock Universe
* **Universe CSV:** A CSV file (path configured in TOML as `universe_file`) containing a single `ric` column.
* **Lot Sizes:** Call `desktool.get_lot_size_table()` at startup for universe RICs. RICs with no lot size are skipped with a warning.
* **Price Mode:** Determined at runtime from the Risk Appetite feed (`buy_state`, `sell_state`).

### C. Config Structure (`configs/config.toml`)

The market name is the TOML section name. All settings live under it — no separate `[market_settings]` section. Per-stock overrides use `[overrides.stock_limit.{market}]`.

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

Config loader (`configs/config.py`) reads the TOML, resolves overrides, and returns a `MarketConfig` object.

## 4. Universe DataFrame

All per-stock state lives in a single pandas DataFrame indexed by `ric`. This replaces per-stock objects and enables vectorized computation.

**Columns:**
| Column | Type | Source | Description |
|--------|------|--------|-------------|
| `lot_size` | int | desktool (startup) | Minimum tradeable lot |
| `stock_limit` | float | TOML config (startup) | Single-name limit |
| `buy_state` / `sell_state` | str | risk appetite feed | Price type (BEST_BID / MID / BEST_OFFER) |
| `buy_raw` / `sell_raw` | float | risk appetite feed | Raw quantities from KDB+ |
| `last_price` | float | live price feed (KDB+ tick) | Stock price in local currency |
| `fx_rate` | float | risk appetite feed | Local currency → USD |
| `alpha` | float | alpha feed | Alpha signal ∈ [-1, 1] |
| `inventory` | float | inventory feed | Current position |
| `live_buy_qty` / `live_sell_qty` | float | dispatch | Currently dispatched quantities |
| `last_sent_time` | datetime | dispatch | Last dispatch time |
| `filled_buy_since_dispatch` | float | fill event | Cumulative buy fills since last full batch |
| `filled_sell_since_dispatch` | float | fill event | Cumulative sell fills since last full batch |
| `pnl_buy_qty` | float | fill event | Cumulative bought qty today (reset on session start) |
| `pnl_buy_cost` | float | fill event | Cumulative buy cost today (fill_price × fill_qty) |
| `pnl_sell_qty` | float | fill event | Cumulative sold qty today (reset on session start) |
| `pnl_sell_revenue` | float | fill event | Cumulative sell revenue today (fill_price × fill_qty) |

**Thread safety:** No locks needed. Feed threads push into `asyncio.Queue` via `call_soon_threadsafe()`. Only the engine's single-threaded async loop reads/writes the DataFrame. GUI receives a copy via `mp.Queue`.

## 5. Feed Interfaces

All feeds push `pd.DataFrame` via threaded callbacks into the engine's async queue.

| Feed | Source | Columns |
|------|--------|---------|
| Risk Appetite | desktool (KDB+) | `ric, buy_state, buy_qty, sell_state, sell_qty, fx_rate` |
| Live Price | KDB+ (tick) | `ric, last_price` |
| Inventory | desktool (KDB+) | `ric, inventory` |
| Trade Fills | desktool (KDB+) | `ric, side, fill_qty, fill_price, timestamp` |
| Alpha | alphaflow | `ric, alpha` (float in [-1, 1]) |

`last_price` is the stock price in local currency, received via a dedicated real-time KDB+ tick subscription. `fx_rate` converts local → USD. Notional = `qty × last_price × fx_rate`.

## 6. Core Logic: The 4-Step Sizing Pipeline

All steps are vectorized pandas operations on the universe DataFrame — no per-stock loops. This produces the "optimal quote" — what we'd want to trade based on current inputs, unconstrained by inventory.

1. **Alpha Skew:** `buy *= (1 + alpha)`, `sell *= (1 - alpha)`. Alpha ∈ [-1, 1].
2. **Single-Name Limit:** `clip(upper=stock_limit)` per stock. Override via `[overrides.stock_limit.{market}]`, else market default `single_name_cap`.
3. **Notional Scaling:** Compute total buy/sell notional (USD). If over limit, scale all down proportionally. Scaling factors ∈ (0, 1].
4. **Lot Size Rounding:** `floor(qty / lot_size) * lot_size`.

**Inventory constraint** is applied separately at dispatch time (after scaling), not in the pipeline:
* `sell_dispatch = min(sell_optimal, max(0, inventory))` — no short selling.

## 7. Two-Mode Dispatch System

### A. Full Batch (Scheduled)
* Runs every `full_batch_interval` minutes (configurable).
* Recomputes the entire 4-step pipeline on all stocks with fresh scaling factors.
* Applies inventory constraint.
* Dispatches all stocks. Updates live state.
* Resets `filled_buy/sell_since_dispatch` to 0 for all stocks.
* Saves scaling factors for partial updates to reuse.

### B. Partial Update (Reactive)
* Triggered by any feed change, subject to `min_dispatch_interval` cooldown (configurable, default 5s).
* Runs the 4-step pipeline using scaling factors from the last full batch.
* Applies inventory constraint.
* Selects stocks to dispatch based on two criteria:
  - **Quote change:** `|optimal - live| / live > partial_change_threshold` (e.g. 10%)
  - **Refill:** `filled_since_dispatch >= refill_fill_threshold * live_qty` (e.g. 50%)
* For refill stocks: dispatch qty capped at `optimal - filled_since_last_full_dispatch` (prevents total execution from exceeding optimal across multiple refills).
* If partial would breach notional limit → auto-promotes to full batch.

### C. Dispatch Decision Flow
```
1. Cooldown active? → skip
2. Full batch due?  → full batch
3. Compute partial  → nothing qualifies? → skip
4. Notional check   → would breach? → full batch
5. Otherwise        → send partial
```

## 8. Refill Logic

Refill is one case of partial update, triggered when fills exceed a threshold.

* **On fill:** Accumulate `fill_qty` into `filled_buy/sell_since_dispatch`. Do NOT reduce `live_buy/sell_qty` — the dark pool tracks remaining quantity internally.
* **Trigger:** `filled_since_dispatch >= refill_fill_threshold * live_qty` (e.g. 50% filled).
* **Amount:** Compute normal partial update quantity (from 4-step pipeline), then cap at `optimal - filled_since_last_full_dispatch`. This ensures total execution does not exceed the optimal across multiple refills.
* **Reset:** `filled_since_dispatch` is only reset on full batch dispatch, not on partial. This tracks cumulative fills across the entire full batch cycle.

## 9. Mark-to-Market PnL

Per-stock PnL is computed from aggregated fill data — no individual fill records stored.

### Accumulators (per stock, reset daily at session start)
* `pnl_buy_qty` / `pnl_buy_cost` — cumulative bought qty and cost (fill_price × fill_qty)
* `pnl_sell_qty` / `pnl_sell_revenue` — cumulative sold qty and revenue

### Formula
* **Local PnL:** `last_price × (pnl_buy_qty - pnl_sell_qty) - pnl_buy_cost + pnl_sell_revenue`
* **USD PnL:** `local_pnl × fx_rate`

This automatically re-evaluates when `last_price` changes — no recomputation loop needed.

### Price Source
* `last_price` from a dedicated real-time KDB+ tick subscription (Live Price feed).

### Daily Reset
All 4 accumulators are zeroed at session start (when `now_active and not was_active` in `_session_monitor`).

## 10. Session Management
* **Session windows:** `"HH:MM-HH:MM"` strings (start inclusive, end exclusive). Multiple sessions per day.
* **Session start:** Immediate full batch dispatch.
* **Session end:** Cancel-all batch (zero-qty for all stocks).
* **Order validity:** `order_valid_time` (minutes) with `refresh_buffer` (seconds) for proactive refresh.

## 11. GUI Specification (The Dashboard)

The GUI runs in a **separate process** using a `multiprocessing.Queue` to receive `EngineSnapshot` updates (containing a copy of the universe DataFrame) with zero impact on trading latency.

### Layout:
* **Theme:** Dark mode (`pyqtdarktheme`).
* **Scaling Banner (top):** Global buy/sell scaling factors. Orange highlight when < 1.0.
* **Main Table (Live Quotes):**
    * Columns: `RIC`, `Bid State`, `Bid Qty`, `Offer State`, `Offer Qty`, `Last Price`, `Inventory`, `Alpha`, `PnL`, `Filled Since Dispatch`.
    * Alpha cells color-coded: green (> 0.05), red (< -0.05).
    * PnL = mark-to-market per stock in local currency.
* **PnL Panel:** Aggregate mark-to-market PnL in local currency and USD.
* **Trade Flow Log:** A scrolling list showing the last 50 events (Fills and Dispatched Batches).
* **Status Bar:** Connection status for KDB+ and Alpha feeds; session countdown timer.

## 12. Safety & Watchdog
* **Heartbeat:** Monitor KDB+ feed staleness. If `> max_staleness`, stop quoting and trigger `winsound`.
* **Session Termination:** On session end, send a "cancel all" (zero-size) batch.
* **Notional Limit:** Total dispatched notional (USD) per side must not exceed `max_buy/sell_notional`. Enforced by scaling in full batch and notional check in partial update.
