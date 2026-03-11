# Custom GUI widgets for the pimm dashboard

from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QGroupBox,
    QLabel,
    QTableWidgetItem,
    QVBoxLayout,
)

# Color constants
ALPHA_POS_COLOR = QColor(0, 180, 0, 60)
ALPHA_NEG_COLOR = QColor(220, 50, 50, 60)
NORMAL_COLOR = QColor(0, 0, 0, 0)


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


class PnlPanel(QGroupBox):
    # Panel displaying aggregate mark-to-market PnL

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
