# Web Dashboard — Layout & UI Reference

This document describes the web dashboard UI in detail, split out from pimm.md for maintainability.

## 1. Technology

- **Server:** FastAPI + uvicorn, sharing the engine's asyncio event loop (`loop="none"`)
- **Transport:** WebSocket for real-time data push (JSON snapshots at ~100ms) and command relay
- **Frontend:** Single-file vanilla HTML + CSS + JS (`pimm/web/static/index.html`), no framework
- **Auth:** UUID token in URL query param (`?token={uuid}`), validated on both HTTP and WebSocket
- **Theme:** Dark mode (Catppuccin-inspired color palette)

### Key Implementation Details

- `uvicorn[standard]` required for WebSocket support (includes `websockets` library)
- `server.install_signal_handlers = lambda: None` prevents uvicorn from overriding Ctrl+C
- Token validated via `request.query_params.get("token")` / `ws.query_params.get("token")`
- DOM controls built once with `addEventListener` (not innerHTML replacement) for button responsiveness
- Client IP logged on WebSocket connect, disconnect, and all commands (e.g. `Command 'start' for [HK] from 192.168.1.50`)

## 2. Layout Structure

The dashboard is a vertical flex container with three sections and resizable dividers between them.

```
+------------------------------------------------------+
|  TOP (auto height)                                    |
|  +---------------------++--------------------------+ |
|  | Control (auto-width) || Summary (fills remaining)| |
|  +---------------------++--------------------------+ |
+------------------------------------------------------+
|  ═══════════ resize handle (top ↔ mid) ═══════════   |
+------------------------------------------------------+
|  MIDDLE (flex: 0 0 200px)                             |
|  +---------------------++--------------------------+ |
|  | Delta and Beta      || Log                      | |
|  | (syncs to Control   || (fills remaining)        | |
|  |  width on load)     ||                          | |
|  +---------------------++--------------------------+ |
+------------------------------------------------------+
|  ═══════════ resize handle (mid ↔ bot) ═══════════   |
+------------------------------------------------------+
|  BOTTOM (flex: 1, fills remaining)                    |
|  [Quoting] [Trades]         Country: [▼] RIC: [___]  |
|  +--------------------------------------------------+|
|  |  Tab content (quoting table or trades split)      ||
|  +--------------------------------------------------+|
+------------------------------------------------------+
```

### Section Sizing

| Section | Vertical | Horizontal |
|---------|----------|------------|
| **Top** | Auto (content height) | Control: auto-width to content; Summary: fills remaining |
| **Middle** | Fixed 200px initial | Delta and Beta: syncs to Control panel width on first data; Log: fills remaining |
| **Bottom** | `flex: 1` (fills remaining) | Full width |

All dividers are draggable for user resize. Resizing uses flex ratios so layout adapts to browser window changes.

## 3. Top Section

### Control Panel (left)

Tab title: **Control**

Per-market row with:
- Market name (e.g., HK, CN, TW) in yellow
- Session time display (e.g., `09:30-12:00,13:00-16:00`)
- Status badge: **ACTIVE** (green) or **INACTIVE** (gray)
- Buttons: **Start**, **Stop**, **View Params**, **Reload Params**

**Button behavior:**
- **Start:** If `day_type == 0`, or `day_type == 0.5` and past the first session window, the frontend shows a JS confirmation dialog (e.g., "Non-trading day — start quoting for HK (session 13:00-16:00)?"). On confirm (or when no warning needed), sends start command. Engine activates the current or next upcoming session window, starts per-market feeds, and dispatches a full batch. The `day_type` value is available in the snapshot data for frontend logic.
- **Stop:** Sends cancel-all (zeros all live quotes), sets `session_active = False`, stops per-market feeds.
- **View Params:** Opens a modal showing the market's current config (sessions, order validity, intervals, caps, thresholds, alpha_enabled). Does not show derived/runtime values like scaling or stock count.
- **Reload Params:** Re-reads `config.cfg` from disk for that market. Preserves current session override (e.g., simulator's `00:00-23:59` is not overwritten). Preserves current active/inactive status.

### Summary Panel (right)

Tab title: **Summary**

Table format with columns: one per market + **Total** column.

| Row | Per-market value | Total |
|-----|-----------------|-------|
| **Scaling** | `buy_scale / sell_scale` (4 decimal places) | `--` |
| **Notional** | `buy_notional / sell_notional` (integer, no $ sign) | sum of all markets |
| **PnL (USD)** | `$amount` (green if positive, red if negative) | sum of all markets |

Notional values are in USD (qty × last_price × fx_rate). Format: `number / number` for buy/sell.

## 4. Middle Section

### Delta and Beta Panel (left)

Tab title: **Delta and Beta**

Displays the latest string from `desktool.get_delta_beta_info()`, queried at `delta_beta_interval` seconds. Empty when no data available. Uses flex layout with internal scroll.

**Filter** (right-aligned in header):
- **Text** input: regex filter (case-insensitive) — filters lines of the delta/beta text, showing only matching lines.

### Log Panel (right)

Tab title: **Log**

Scrolling log area showing engine log messages (INFO+ captured via `WebLogHandler`). Stores up to 500 structured `{level, msg}` entries. Auto-scrolls to bottom on new entries. Uses flex layout with internal scroll — no outer scrollbar.

**Filters** (right-aligned in header, matching Country/RIC filter style):
- **Level** dropdown: DEBUG / INFO (default) / WARNING / ERROR / CRITICAL — shows logs at or above selected level
- **Text** input: regex filter (case-insensitive) on log message content

**Color coding:** gray=DEBUG, white=INFO, yellow=WARNING, red+bold=ERROR and CRITICAL.

## 5. Bottom Section — Tabs

### Filter Bar

On the tab row, right-aligned:
- **Country** dropdown: `All` (default) + one option per market (auto-populated from data)
- **RIC** text input: regex filter (case-insensitive). Filters quoting table, trade fills, and trade summary. Invalid regex is silently ignored.

### Tab 1: Quoting

Full-width table with draggable column reordering (HTML5 drag API, order persisted to `localStorage`).

**Default column order:**

| # | Column | Key | Alignment |
|---|--------|-----|-----------|
| 1 | RIC | `ric` | left |
| 2 | Status | `status` | left |
| 3 | Alpha | `alpha` | right |
| 4 | Bid State | `buy_state` | left |
| 5 | Ask State | `sell_state` | left |
| 6 | Opt Bid | `buy_raw` | right |
| 7 | Opt Ask | `sell_raw` | right |
| 8 | Live Bid | `live_buy` | right |
| 9 | Live Ask | `live_sell` | right |
| 10 | Filled B | `filled_buy` | right |
| 11 | Filled S | `filled_sell` | right |
| 12 | Inventory | `inventory` | right |
| 13 | Last Price | `last_price` | right (2 decimal places) |
| 14 | Updated | `last_sent` | left |
| 15 | Remark | `remark` | left |

**Sorting:** Rows sorted by RIC suffix (region) first, then alphabetically by RIC within each region. This groups stocks by market (e.g., all `.HK` together, then `.SS`/`.SZ`, then `.TW`).

**Column features:**
- Drag-and-drop reordering (persisted to `localStorage` key `pimm_col_order`)
- Resizable column widths via drag handle on right edge of each header cell

**Formatting:**
- Alpha: green background tint if > 0.05, red tint if < -0.05
- Status: green "ON" or gray "OFF"
- Last Price: 2 decimal places
- Quantities: integer with thousands separator

### Tab 2: Trades

Horizontal split (1:2 ratio — Trade Fills takes 1/3, Trade Summary takes 2/3) with a draggable resize handle between them.

**Left — Trade Fills:**
Scrolling list of recent fills, newest first. Each line:
```
[HH:MM:SS] MARKET SIDE RIC qty=N @ price
```
Color: green for buy, red for sell.

**Right — Trade Summary:**
Per-stock table (only stocks with fills). Columns:
- RIC
- Buy Notional (`pnl_buy_qty × last_price`)
- Sell Notional (`pnl_sell_qty × last_price`)
- PnL (local currency, mark-to-market)
- Ratio (`min(buy, sell) / max(buy, sell) × 100%`)

Column widths are resizable via drag handle on header cells.

## 6. Resizing

### Panel Resize

Four resize handles:
- **Top ↔ Middle** (horizontal bar): adjusts vertical split between top and middle sections
- **Middle ↔ Bottom** (horizontal bar): adjusts vertical split between middle and bottom sections
- **Left ↔ Right** within Top and Middle (vertical bars): adjusts horizontal split
- **Trade Fills ↔ Trade Summary** (vertical bar): adjusts the 1:2 horizontal split within the Trades tab

Resize uses flex ratios: dragging computes the new ratio based on pixel delta, then sets `style.flex` on both panels. This ensures layout adapts proportionally when the browser window is resized.

Minimum sizes: 100px horizontal, 40px vertical.

### Column Resize

Both the quoting table and trade summary table support column width resizing. Each header cell has a 4px drag handle on the right edge. Dragging sets explicit `width` and `minWidth` on the header cell. Minimum column width: 40px.

## 7. Params Modal

Triggered by **View Params** button. Displays a read-only popup with the market's current configuration:

```
Sessions: 09:30-12:00,13:00-16:00
Order Valid Time: 5s
Refresh Buffer: 15s
Full Batch Interval: 10 min
Min Dispatch Interval: 5s
Single Name Cap: 50,000
Max Buy Notional: 10,000,000
Max Sell Notional: 10,000,000
Max Staleness: 30s
Partial Change Threshold: 10.0%
Refill Fill Threshold: 50.0%
Alpha Enabled: true
```

Closed by clicking the **Close** button or clicking the overlay.

## 8. Connection Status

Fixed indicator in top-right corner:
- **Connected** (green) — WebSocket is open
- **Disconnected** (red) — WebSocket is closed, auto-reconnect every 2 seconds
