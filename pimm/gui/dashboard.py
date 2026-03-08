# PyQt6 main dashboard window — reads from DataFrame in EngineSnapshot

from datetime import datetime

import pandas as pd
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QHeaderView,
    QLabel,
    QMainWindow,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pimm.gui.widgets import AlphaItem, PnlPanel, ScalingBanner, TradeFlowLog
from pimm.utils.time import HKT


class DashboardWindow(QMainWindow):
    # Main GUI window for pimm monitoring

    COLUMNS = [
        "RIC", "Bid State", "Bid Qty", "Offer State", "Offer Qty",
        "Last Price", "Inventory", "Alpha", "PnL",
        "Bought Since Full Update", "Sold Since Full Update",
        "Update Time",
    ]

    def __init__(self, queue):
        super().__init__()
        self._queue = queue
        self._latest_snapshot = None

        self.setWindowTitle("pimm — Market Maker Dashboard")
        self.setMinimumSize(1200, 700)

        self._setup_ui()
        self._setup_timer()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Top banner: global scaling factors
        self._scaling_banner = ScalingBanner()
        main_layout.addWidget(self._scaling_banner)

        # Main content: table + right panel
        splitter = QSplitter()
        main_layout.addWidget(splitter)

        # Left: quote table
        self._table = QTableWidget()
        self._table.setColumnCount(len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        splitter.addWidget(self._table)

        # Right panel: PnL + Trade log
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        self._pnl_panel = PnlPanel()
        right_layout.addWidget(self._pnl_panel)
        self._trade_log = TradeFlowLog()
        right_layout.addWidget(self._trade_log)
        splitter.addWidget(right_panel)
        splitter.setSizes([700, 400])

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._session_label = QLabel("Session: --")
        self._feed_label = QLabel("Feeds: --")
        self._full_batch_label = QLabel("Last Full Batch: --")
        self._countdown_label = QLabel("Countdown: --")
        self._status_bar.addWidget(self._session_label)
        self._status_bar.addWidget(self._feed_label)
        self._status_bar.addWidget(self._full_batch_label)
        self._status_bar.addPermanentWidget(self._countdown_label)

    def _setup_timer(self):
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll_queue)
        self._timer.start(100)

    def _poll_queue(self):
        # Read all available snapshots, keep latest
        snapshot = None
        try:
            while not self._queue.empty():
                snapshot = self._queue.get_nowait()
        except Exception:
            pass

        if snapshot is not None:
            self._latest_snapshot = snapshot
            self._update_display(snapshot)

    def _update_display(self, snap):
        self._scaling_banner.update_scaling(snap.buy_scaling, snap.sell_scaling)
        self._update_table(snap)
        self._update_status(snap)
        self._trade_log.update_fills(snap.recent_fills)

        df = snap.universe
        pnl_local = (
            df["last_price"] * (df["pnl_buy_qty"] - df["pnl_sell_qty"])
            - df["pnl_buy_cost"] + df["pnl_sell_revenue"]
        )
        pnl_usd = pnl_local * df["fx_rate"]
        self._pnl_panel.update_pnl(
            local_pnl=pnl_local.sum(), usd_pnl=pnl_usd.sum()
        )

    def _update_table(self, snap):
        # Update main quote table from DataFrame
        df = snap.universe
        rics = sorted(df.index.tolist())
        self._table.setRowCount(len(rics))

        for row_idx, ric in enumerate(rics):
            r = df.loc[ric]
            # Format last dispatch time
            sent = r["last_sent_time"]
            sent_str = sent.strftime("%H:%M:%S") if pd.notna(sent) else "--"

            stock_pnl = (
                r["last_price"] * (r["pnl_buy_qty"] - r["pnl_sell_qty"])
                - r["pnl_buy_cost"] + r["pnl_sell_revenue"]
            )

            values = [
                ric,
                str(r["buy_state"]) if r["buy_state"] else "--",
                format(r["live_buy_qty"], ".0f"),
                str(r["sell_state"]) if r["sell_state"] else "--",
                format(r["live_sell_qty"], ".0f"),
                format(r["last_price"], ",.4f"),
                format(r["inventory"], ".0f"),
                format(r["alpha"], ".4f"),
                format(stock_pnl, ",.2f"),
                format(r["filled_buy_since_dispatch"], ".0f"),
                format(r["filled_sell_since_dispatch"], ".0f"),
                sent_str,
            ]

            for col, val in enumerate(values):
                item = AlphaItem(val, r["alpha"]) if col == 7 else QTableWidgetItem(val)
                self._table.setItem(row_idx, col, item)

    def _update_status(self, snap):
        session_text = "ACTIVE" if snap.session_active else "INACTIVE"
        self._session_label.setText("Session: %s" % session_text)

        if snap.session_end_countdown is not None:
            mins = int(snap.session_end_countdown // 60)
            secs = int(snap.session_end_countdown % 60)
            self._countdown_label.setText("Ends in: %02d:%02d" % (mins, secs))
        else:
            self._countdown_label.setText("Countdown: --")

        # Last full batch time
        if snap.last_full_batch_time is not None:
            fb_dt = datetime.fromtimestamp(
                snap.last_full_batch_time, tz=HKT
            )
            self._full_batch_label.setText(
                "Last Full Batch: %s" % fb_dt.strftime("%H:%M:%S")
            )
        else:
            self._full_batch_label.setText("Last Full Batch: --")

        feed_parts = []
        for name, status in snap.feed_status.items():
            feed_parts.append("%s: %s" % (name, status))
        self._feed_label.setText(
            "Feeds: %s" % ", ".join(feed_parts) if feed_parts else "Feeds: --"
        )
