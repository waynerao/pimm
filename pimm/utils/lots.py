import logging

logger = logging.getLogger(__name__)


def build_lot_size_table(df):
    # Convert desktool lot size DataFrame to a {ric: lot_size} dict
    table = {}
    for _, row in df.iterrows():
        ric = str(row["ric"])
        lot_size = int(row["lot_size"])
        if lot_size <= 0:
            logger.warning("Invalid lot_size %d for %s, skipping", lot_size, ric)
            continue
        table[ric] = lot_size
    return table
