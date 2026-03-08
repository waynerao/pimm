# Custom GUI widgets for the pimm dashboard

from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# Color constants
SCALED_COLOR = QColor(255, 165, 0, 80)    # Orange tint
NORMAL_COLOR = QColor(0, 0, 0, 0)         # Transparent
ALPHA_POS_COLOR = QColor(0, 180, 0, 60)   # Green tint (bullish)
ALPHA_NEG_COLOR = QColor(220, 50, 50, 60) # Red tint (bearish)


class AlphaItem(QTableWidgetItem):
    # Table item that color-codes alpha: green=positive, red=negative

    def __init__(self, text, alpha):
        super().__init__(text)
        if alpha > 0.05:
            self.setBackground(QBrush(ALPHA_POS_COLOR))
        elif alpha < -0.05:
            self.setBackground(QBrush(ALPHA_NEG_COLOR))
        else:
            self.setBackground(QBrush(NORMAL_COLOR))


class ScalingBanner(QWidget):
    # Top banner showing global buy/sell scaling factors

    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        bold = QFont()
        bold.setBold(True)

        title = QLabel("Global Scaling:")
        title.setFont(bold)
        layout.addWidget(title)

        self._buy_label = QLabel("Buy: 1.00")
        self._buy_label.setFont(bold)
        self._buy_label.setMinimumWidth(120)
        layout.addWidget(self._buy_label)

        self._sell_label = QLabel("Sell: 1.00")
        self._sell_label.setFont(bold)
        self._sell_label.setMinimumWidth(120)
        layout.addWidget(self._sell_label)

        layout.addStretch()

    def update_scaling(self, buy, sell):
        self._buy_label.setText("Buy: %.4f" % buy)
        self._sell_label.setText("Sell: %.4f" % sell)

        buy_color = "orange" if buy < 1.0 else "white"
        sell_color = "orange" if sell < 1.0 else "white"

        self._buy_label.setStyleSheet("color: %s;" % buy_color)
        self._sell_label.setStyleSheet("color: %s;" % sell_color)


class PnlPanel(QGroupBox):
    # Panel displaying mark-to-market PnL in local currency and USD

    def __init__(self):
        super().__init__("PnL")
        layout = QVBoxLayout(self)
        self._local_label = QLabel("Local PnL: --")
        self._usd_label = QLabel("USD PnL: --")
        layout.addWidget(self._local_label)
        layout.addWidget(self._usd_label)

    def update_pnl(self, local_pnl=None, usd_pnl=None):
        if local_pnl is not None:
            self._local_label.setText("Local PnL: %s" % format(local_pnl, ",.2f"))
        if usd_pnl is not None:
            self._usd_label.setText("USD PnL: %s" % format(usd_pnl, ",.2f"))


class TradeFlowLog(QGroupBox):
    # Scrolling log showing recent trade fills and dispatched batches

    MAX_LINES = 50

    def __init__(self):
        super().__init__("Trade Flow")
        layout = QVBoxLayout(self)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(250)
        layout.addWidget(self._log)
        self._displayed_count = 0

    def update_fills(self, fills):
        if len(fills) <= self._displayed_count:
            return
        new_fills = fills[self._displayed_count:]
        for fill in new_fills:
            line = "[%s] %s %s qty=%.0f @ %.4f" % (
                fill.timestamp.strftime("%H:%M:%S"),
                fill.side.value.upper(),
                fill.ric,
                fill.fill_qty,
                fill.fill_price,
            )
            self._log.append(line)
        self._displayed_count = len(fills)

    def add_dispatch_event(self, count):
        self._log.append("[DISPATCH] Batch sent: %d quotes" % count)
