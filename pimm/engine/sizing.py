# 4-step vectorized sizing pipeline — all operations on DataFrames
# Step 1: Alpha skew
# Step 2: Single-name cap
# Step 3: Notional scaling
# Step 4: Lot size rounding
#
# Inventory constraint is applied separately at dispatch time.


def compute_optimal_quotes(df, max_buy_notional, max_sell_notional):
    # Full pipeline with fresh scaling factors (for full batch)
    # Returns (result_df, buy_scaling, sell_scaling)
    result = df[["buy_raw", "sell_raw", "alpha", "stock_limit", "last_price", "fx_rate", "lot_size"]].copy()

    # Step 1: Alpha skew
    result["buy_skewed"] = result["buy_raw"] * (1.0 + result["alpha"])
    result["sell_skewed"] = result["sell_raw"] * (1.0 - result["alpha"])

    # Step 2: Single-name cap
    result["buy_capped"] = result["buy_skewed"].clip(upper=result["stock_limit"])
    result["sell_capped"] = result["sell_skewed"].clip(upper=result["stock_limit"])

    # Step 3: Notional scaling (compute fresh factors)
    price_fx = result["last_price"] * result["fx_rate"]
    buy_notional = (result["buy_capped"] * price_fx).sum()
    sell_notional = (result["sell_capped"] * price_fx).sum()

    buy_scaling = min(1.0, max_buy_notional / buy_notional) if buy_notional > 0 else 1.0
    sell_scaling = min(1.0, max_sell_notional / sell_notional) if sell_notional > 0 else 1.0

    result["buy_scaled"] = result["buy_capped"] * buy_scaling
    result["sell_scaled"] = result["sell_capped"] * sell_scaling

    # Step 4: Lot rounding
    lot = result["lot_size"]
    result["buy_optimal"] = (result["buy_scaled"] // lot) * lot
    result["sell_optimal"] = (result["sell_scaled"] // lot) * lot

    return result[["buy_optimal", "sell_optimal"]], buy_scaling, sell_scaling


def compute_optimal_cached(df, buy_scaling, sell_scaling):
    # Pipeline with cached scaling factors (for partial updates)
    # Returns result_df with buy_optimal, sell_optimal columns
    result = df[["buy_raw", "sell_raw", "alpha", "stock_limit", "lot_size"]].copy()

    # Step 1: Alpha skew
    result["buy_skewed"] = result["buy_raw"] * (1.0 + result["alpha"])
    result["sell_skewed"] = result["sell_raw"] * (1.0 - result["alpha"])

    # Step 2: Single-name cap
    result["buy_capped"] = result["buy_skewed"].clip(upper=result["stock_limit"])
    result["sell_capped"] = result["sell_skewed"].clip(upper=result["stock_limit"])

    # Step 3: Notional scaling (use cached factors)
    result["buy_scaled"] = result["buy_capped"] * buy_scaling
    result["sell_scaled"] = result["sell_capped"] * sell_scaling

    # Step 4: Lot rounding
    lot = result["lot_size"]
    result["buy_optimal"] = (result["buy_scaled"] // lot) * lot
    result["sell_optimal"] = (result["sell_scaled"] // lot) * lot

    return result[["buy_optimal", "sell_optimal"]]


def apply_inventory_constraint(optimal_df, universe_df):
    # Apply inventory constraint at dispatch time
    # sell_dispatch = min(sell_optimal, max(0, inventory))
    # buy_dispatch = buy_optimal (unaffected)
    dispatch = optimal_df[["buy_optimal", "sell_optimal"]].copy()
    dispatch.rename(columns={"buy_optimal": "buy_dispatch", "sell_optimal": "sell_dispatch"}, inplace=True)
    inv_floor = universe_df["inventory"].clip(lower=0)
    dispatch["sell_dispatch"] = dispatch["sell_dispatch"].clip(upper=inv_floor)
    return dispatch
