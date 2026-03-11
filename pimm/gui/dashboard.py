# PyQt6 main dashboard window — multi-market with country controls

import pandas as pd
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pimm.gui.widgets import AlphaItem, PnlPanel


class DashboardWindow(QMainWindow):

    QUOTE_COLUMNS = [
        "RIC", "Status", "Remark", "Bid State", "Bid Qty",
        "Offer State", "Offer Qty", "Last Price", "Inventory",
        "Alpha", "PnL", "Bought Since Dispatch", "Sold Since Dispatch",
        "Update Time",
    ]

    def __init__(self, data_queue, cmd_queue=None):
        super().__init__()
        self._data_queue = data_queue
        self._cmd_queue = cmd_queue
        self._latest_snapshot = None
        self._market_controls = {}

        self.setWindowTitle("pimm — Market Maker Dashboard")
        self.setMinimumSize(1400, 800)

        self._setup_ui()
        self._setup_timer()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Top area: controls (left) + summary (right)
        top_splitter = QSplitter()
        main_layout.addWidget(top_splitter)

        # Top left: country control panel
        self._controls_panel = QGroupBox("Market Controls")
        self._controls_layout = QVBoxLayout(self._controls_panel)
        self._controls_layout.addWidget(QLabel("(waiting for data...)"))
        top_splitter.addWidget(self._controls_panel)

        # Top right: global summary
        summary_panel = QGroupBox("Global Summary")
        summary_layout = QVBoxLayout(summary_panel)
        self._scaling_label = QLabel("Scaling: --")
        self._notional_label = QLabel("Notional: --")
        self._pnl_panel = PnlPanel()
        summary_layout.addWidget(self._scaling_label)
        summary_layout.addWidget(self._notional_label)
        summary_layout.addWidget(self._pnl_panel)
        summary_layout.addStretch()
        top_splitter.addWidget(summary_panel)
        top_splitter.setSizes([600, 500])

        # Middle: quoting table with country filter
        mid_widget = QWidget()
        mid_layout = QVBoxLayout(mid_widget)
        mid_layout.setContentsMargins(0, 4, 0, 0)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Country Filter:"))
        self._country_filter = QComboBox()
        self._country_filter.addItem("All")
        self._country_filter.currentTextChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._country_filter)
        filter_row.addStretch()
        mid_layout.addLayout(filter_row)

        self._table = QTableWidget()
        self._table.setColumnCount(len(self.QUOTE_COLUMNS))
        self._table.setHorizontalHeaderLabels(self.QUOTE_COLUMNS)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        mid_layout.addWidget(self._table)
        main_layout.addWidget(mid_widget)

        # Bottom: trade fills
        fills_group = QGroupBox("Trade Fills")
        fills_layout = QVBoxLayout(fills_group)
        self._fills_log = QTextEdit()
        self._fills_log.setReadOnly(True)
        self._fills_log.setMaximumHeight(200)
        fills_layout.addWidget(self._fills_log)
        main_layout.addWidget(fills_group)
        self._displayed_fill_count = 0

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Status: --")
        self._status_bar.addWidget(self._status_label)

    def _setup_timer(self):
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll_queue)
        self._timer.start(100)

    def _poll_queue(self):
        snapshot = None
        try:
            while not self._data_queue.empty():
                snapshot = self._data_queue.get_nowait()
        except Exception:
            pass

        if snapshot is not None:
            self._latest_snapshot = snapshot
            self._update_display(snapshot)

    def _update_display(self, snap):
        self._ensure_market_controls(snap)
        self._update_controls(snap)
        self._update_summary(snap)
        self._update_table(snap)
        self._update_fills(snap)
        self._update_status(snap)

    def _ensure_market_controls(self, snap):
        # Build market control rows on first snapshot
        if self._market_controls:
            return

        # Clear placeholder
        while self._controls_layout.count():
            item = self._controls_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        for mname in sorted(snap.markets.keys()):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(2, 2, 2, 2)

            lbl = QLabel(mname)
            lbl.setMinimumWidth(40)
            row_layout.addWidget(lbl)

            session_lbl = QLabel("--")
            session_lbl.setMinimumWidth(120)
            row_layout.addWidget(session_lbl)

            status_lbl = QLabel("--")
            status_lbl.setMinimumWidth(80)
            row_layout.addWidget(status_lbl)

            start_btn = QPushButton("Start")
            start_btn.clicked.connect(
                lambda checked, m=mname: self._send_cmd("start", m)
            )
            row_layout.addWidget(start_btn)

            stop_btn = QPushButton("Stop")
            stop_btn.clicked.connect(lambda checked, m=mname: self._send_cmd("stop", m))
            row_layout.addWidget(stop_btn)

            view_btn = QPushButton("View Params")
            view_btn.clicked.connect(lambda checked, m=mname: self._view_params(m))
            row_layout.addWidget(view_btn)

            reload_btn = QPushButton("Reload")
            reload_btn.clicked.connect(
                lambda checked, m=mname: self._send_cmd("reload", m)
            )
            row_layout.addWidget(reload_btn)

            self._controls_layout.addWidget(row)
            self._market_controls[mname] = {
                "session_lbl": session_lbl,
                "status_lbl": status_lbl,
            }

            # Add to country filter
            self._country_filter.addItem(mname)

        self._controls_layout.addStretch()

    def _update_controls(self, snap):
        for mname, ctrl in self._market_controls.items():
            active = snap.session_status.get(mname, False)
            ctrl["status_lbl"].setText("ACTIVE" if active else "INACTIVE")
            color = "lime" if active else "gray"
            ctrl["status_lbl"].setStyleSheet("color: %s;" % color)

            countdown = snap.session_countdowns.get(mname)
            if countdown is not None:
                mins = int(countdown // 60)
                secs = int(countdown % 60)
                ctrl["session_lbl"].setText("Ends: %02d:%02d" % (mins, secs))
            else:
                ctrl["session_lbl"].setText("No session")

    def _update_summary(self, snap):
        # Scaling per market
        scaling_parts = []
        for mname in sorted(snap.scaling.keys()):
            bs, ss = snap.scaling[mname]
            scaling_parts.append("%s: B=%.4f S=%.4f" % (mname, bs, ss))
        self._scaling_label.setText("Scaling: %s" % " | ".join(scaling_parts))

        # Aggregate PnL across all markets
        total_local = 0.0
        total_usd = 0.0
        notional_parts = []
        for mname in sorted(snap.markets.keys()):
            df = snap.markets[mname]
            pnl_local = (
                df["last_price"] * (df["pnl_buy_qty"] - df["pnl_sell_qty"])
                - df["pnl_buy_cost"] + df["pnl_sell_revenue"]
            )
            pnl_usd = pnl_local * df["fx_rate"]
            total_local += pnl_local.sum()
            total_usd += pnl_usd.sum()

            buy_not = (df["live_buy_qty"] * df["last_price"] * df["fx_rate"]).sum()
            sell_not = (df["live_sell_qty"] * df["last_price"] * df["fx_rate"]).sum()
            notional_parts.append(
                "%s: B=$%s S=$%s" % (
                    mname, format(buy_not, ",.0f"),
                    format(sell_not, ",.0f"),
                )
            )

        self._pnl_panel.update_pnl(local_pnl=total_local, usd_pnl=total_usd)
        self._notional_label.setText("Notional: %s" % " | ".join(notional_parts))

    def _update_table(self, snap):
        selected_market = self._country_filter.currentText()

        all_rows = []
        for mname in sorted(snap.markets.keys()):
            if selected_market != "All" and mname != selected_market:
                continue
            df = snap.markets[mname]
            for ric in sorted(df.index.tolist()):
                r = df.loc[ric]
                all_rows.append((ric, r, mname))

        self._table.setRowCount(len(all_rows))

        for row_idx, (ric, r, _mname) in enumerate(all_rows):
            sent = r["last_sent_time"]
            sent_str = sent.strftime("%H:%M:%S") if pd.notna(sent) else "--"

            stock_pnl = (
                r["last_price"] * (r["pnl_buy_qty"] - r["pnl_sell_qty"])
                - r["pnl_buy_cost"] + r["pnl_sell_revenue"]
            )

            status_str = "ON" if r["quote_status"] else "OFF"
            remark_str = str(r["remark"]) if r["remark"] else ""

            values = [
                ric,
                status_str,
                remark_str,
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
                item = (
                    AlphaItem(val, r["alpha"])
                    if col == 9
                    else QTableWidgetItem(val)
                )
                self._table.setItem(row_idx, col, item)

    def _update_fills(self, snap):
        fills = snap.recent_fills
        selected_market = self._country_filter.currentText()

        if len(fills) <= self._displayed_fill_count:
            return

        new_fills = fills[self._displayed_fill_count:]
        for fill in new_fills:
            mname = self._ric_to_market_name(fill.ric, snap)
            if selected_market != "All" and mname != selected_market:
                continue
            line = "[%s] %s %s %s qty=%.0f @ %.4f" % (
                fill.timestamp.strftime("%H:%M:%S"),
                mname or "??",
                fill.side.value.upper(),
                fill.ric,
                fill.fill_qty,
                fill.fill_price,
            )
            self._fills_log.append(line)
        self._displayed_fill_count = len(fills)

    def _update_status(self, snap):
        active_markets = [m for m, a in snap.session_status.items() if a]
        if active_markets:
            self._status_label.setText(
                "Active: %s" % ", ".join(sorted(active_markets))
            )
        else:
            self._status_label.setText("No active sessions")

    def _ric_to_market_name(self, ric, snap):
        for mname, df in snap.markets.items():
            if ric in df.index:
                return mname
        return None

    def _send_cmd(self, action, market_name):
        if self._cmd_queue is not None:
            self._cmd_queue.put_nowait((action, market_name))

    def _view_params(self, market_name):
        snap = self._latest_snapshot
        if snap is None:
            return
        df = snap.markets.get(market_name)
        if df is None:
            return
        # Show a simple message with current state summary
        scaling = snap.scaling.get(market_name, (1.0, 1.0))
        active = snap.session_status.get(market_name, False)
        text = (
            "Market: %s\n"
            "Session: %s\n"
            "Buy Scaling: %.4f\n"
            "Sell Scaling: %.4f\n"
            "Stocks: %d (quoting: %d)\n"
        ) % (
            market_name,
            "ACTIVE" if active else "INACTIVE",
            scaling[0], scaling[1],
            len(df),
            int(df["quote_status"].sum()),
        )
        QMessageBox.information(self, "Params: %s" % market_name, text)

    def _on_filter_changed(self, text):
        # Re-render table and fills with new filter
        if self._latest_snapshot is not None:
            self._displayed_fill_count = 0
            self._fills_log.clear()
            self._update_table(self._latest_snapshot)
            self._update_fills(self._latest_snapshot)
