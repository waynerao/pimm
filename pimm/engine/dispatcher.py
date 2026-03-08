# Batch construction and dispatch — full batch + partial update modes

import logging

import pandas as pd

from pimm.engine.refill import (
    cap_refill_qty,
    get_refill_mask,
    reset_fill_counters,
)
from pimm.engine.sizing import (
    apply_inventory_constraint,
    compute_optimal_cached,
    compute_optimal_quotes,
)
from pimm.utils.time import now_hkt

logger = logging.getLogger(__name__)


def build_full_batch(state_mgr, config):
    # Build a full batch: all stocks, with fresh notional scaling
    # Returns (dispatch_df, buy_scaling, sell_scaling)
    # dispatch_df has columns: buy_dispatch, sell_dispatch, buy_state, sell_state
    df = state_mgr.df

    # Run 4-step pipeline with fresh scaling
    optimal, buy_scaling, sell_scaling = compute_optimal_quotes(
        df, config.max_buy_notional, config.max_sell_notional
    )

    # Apply inventory constraint
    dispatch = apply_inventory_constraint(optimal, df)

    # Attach price types for output
    dispatch["buy_state"] = df["buy_state"]
    dispatch["sell_state"] = df["sell_state"]

    # Update live state in universe DataFrame
    now = now_hkt()
    df["live_buy_qty"] = dispatch["buy_dispatch"]
    df["live_sell_qty"] = dispatch["sell_dispatch"]
    df["last_sent_time"] = now

    # Reset fill counters on full batch
    reset_fill_counters(df)

    return dispatch, buy_scaling, sell_scaling


def build_partial_update(state_mgr, config, buy_scaling, sell_scaling):
    # Build a partial update using cached scaling factors
    # Returns dispatch_df (only changed stocks) or None if nothing qualifies
    df = state_mgr.df

    # Compute optimal with cached scaling
    optimal = compute_optimal_cached(df, buy_scaling, sell_scaling)

    # Apply inventory constraint
    dispatch = apply_inventory_constraint(optimal, df)

    # Determine which stocks qualify for update
    # Case A: Quote change exceeds threshold
    threshold = config.partial_change_threshold
    buy_change = _relative_change(dispatch["buy_dispatch"], df["live_buy_qty"])
    sell_change = _relative_change(dispatch["sell_dispatch"], df["live_sell_qty"])
    quote_changed = (buy_change > threshold) | (sell_change > threshold)

    # Case B: Refill triggered
    refill_mask = get_refill_mask(df, config.refill_fill_threshold)

    # Union of both cases
    update_mask = quote_changed | refill_mask

    if not update_mask.any():
        return None

    partial = dispatch[update_mask].copy()

    # Cap refill stocks
    refill_rics = refill_mask & update_mask
    if refill_rics.any():
        partial.loc[refill_rics] = cap_refill_qty(
            partial.loc[refill_rics], df.loc[refill_rics.index[refill_rics]]
        )

    # Attach price types
    partial["buy_state"] = df.loc[partial.index, "buy_state"]
    partial["sell_state"] = df.loc[partial.index, "sell_state"]

    return partial


def apply_partial_to_state(state_mgr, partial_df):
    # Update universe DataFrame live quantities for partial update stocks
    now = now_hkt()
    for ric in partial_df.index:
        state_mgr.df.at[ric, "live_buy_qty"] = partial_df.at[ric, "buy_dispatch"]
        state_mgr.df.at[ric, "live_sell_qty"] = partial_df.at[ric, "sell_dispatch"]
        state_mgr.df.at[ric, "last_sent_time"] = now


def compute_notional_impact(universe_df, partial_df):
    # Compute expected total notional if partial updates are applied
    # Returns (buy_notional_usd, sell_notional_usd)
    partial_rics = set(partial_df.index)

    # Current live notional for stocks NOT in partial
    unch = universe_df.loc[~universe_df.index.isin(partial_rics)]
    buy_live = (
        unch["live_buy_qty"] * unch["last_price"] * unch["fx_rate"]
    ).sum()
    sell_live = (
        unch["live_sell_qty"] * unch["last_price"] * unch["fx_rate"]
    ).sum()

    # Add partial update notional
    pp = universe_df.loc[partial_df.index]
    buy_partial = (
        partial_df["buy_dispatch"] * pp["last_price"] * pp["fx_rate"]
    ).sum()
    sell_partial = (
        partial_df["sell_dispatch"] * pp["last_price"] * pp["fx_rate"]
    ).sum()

    return buy_live + buy_partial, sell_live + sell_partial


def build_cancel_all(state_mgr):
    # Build zero-qty cancel-all DataFrame for session end
    df = state_mgr.df
    cancel = pd.DataFrame(index=df.index)
    cancel["buy_dispatch"] = 0.0
    cancel["sell_dispatch"] = 0.0
    cancel["buy_state"] = df["buy_state"]
    cancel["sell_state"] = df["sell_state"]
    return cancel


def dispatch_to_dataframe(dispatch_df):
    # Convert dispatch DataFrame to output format for KDB+ injector
    out = pd.DataFrame()
    out["ric"] = dispatch_df.index
    out["buy_state"] = dispatch_df["buy_state"].values
    out["buy_qty"] = dispatch_df["buy_dispatch"].values
    out["sell_state"] = dispatch_df["sell_state"].values
    out["sell_qty"] = dispatch_df["sell_dispatch"].values
    return out.reset_index(drop=True)


def _relative_change(new, old):
    # Compute |new - old| / old, handling zeros
    # Returns a Series of relative change values
    change = (new - old).abs()
    # Where old is 0, treat any non-zero new as infinite change (use 1.0)
    result = change / old.replace(0, 1)
    # If old was 0 and new is also 0, change is 0
    result = result.where(~((old == 0) & (new == 0)), 0.0)
    # If old was 0 and new is non-zero, mark as 100% change
    result = result.where(~((old == 0) & (new != 0)), 1.0)
    return result
