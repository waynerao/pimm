# Fill accumulation + refill trigger logic
# - Fills accumulate into filled_buy/sell_since_dispatch
# - Refill triggers when filled >= threshold * live_qty
# - Refill qty capped at optimal - filled_since_dispatch
# - filled_since_dispatch only resets on full batch

import logging

logger = logging.getLogger(__name__)


def accumulate_fills(universe_df, fills_df):
    # Add fill quantities to filled_since_dispatch counters
    # fills_df columns: ric, side, fill_qty, fill_price, timestamp
    for _, row in fills_df.iterrows():
        ric = str(row["ric"])
        if ric not in universe_df.index:
            continue
        side = str(row["side"]).lower()
        qty = float(row["fill_qty"])
        if side == "buy":
            universe_df.at[ric, "filled_buy_since_dispatch"] += qty
        else:
            universe_df.at[ric, "filled_sell_since_dispatch"] += qty

    logger.info(f"Accumulated fills for {len(fills_df)} rows")


def get_refill_mask(universe_df, threshold):
    # Return boolean mask: stocks where filled >= threshold * live_qty
    buy_refill = universe_df["filled_buy_since_dispatch"] >= threshold * universe_df["live_buy_qty"]
    sell_refill = universe_df["filled_sell_since_dispatch"] >= threshold * universe_df["live_sell_qty"]
    # A stock needs refill if either side triggers
    return buy_refill | sell_refill


def cap_refill_qty(dispatch_df, universe_df):
    # Cap dispatch qty at optimal - filled_since_dispatch
    # Ensures total execution does not exceed optimal across multiple refills
    capped = dispatch_df.copy()

    filled_buy = universe_df["filled_buy_since_dispatch"]
    filled_sell = universe_df["filled_sell_since_dispatch"]
    max_buy = (capped["buy_dispatch"] - filled_buy).clip(lower=0)
    max_sell = (capped["sell_dispatch"] - filled_sell).clip(lower=0)

    capped["buy_dispatch"] = capped["buy_dispatch"].clip(upper=max_buy)
    capped["sell_dispatch"] = capped["sell_dispatch"].clip(upper=max_sell)

    return capped


def reset_fill_counters(universe_df):
    # Zero out fill counters (called on full batch)
    universe_df["filled_buy_since_dispatch"] = 0.0
    universe_df["filled_sell_since_dispatch"] = 0.0
