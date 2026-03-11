# GUI process bootstrap — runs in a separate multiprocessing.Process

import logging
import sys
from multiprocessing import Process

logger = logging.getLogger(__name__)


def start_gui_process(data_queue, cmd_queue=None):
    proc = Process(
        target=_gui_main,
        args=(data_queue, cmd_queue),
        name="pimm-gui",
        daemon=True,
    )
    proc.start()
    logger.info("GUI process started (pid=%s)", proc.pid)
    return proc


def _gui_main(data_queue, cmd_queue):
    import qdarktheme
    from PyQt6.QtWidgets import QApplication

    from pimm.gui.dashboard import DashboardWindow

    app = QApplication(sys.argv)
    qdarktheme.setup_theme()

    window = DashboardWindow(data_queue, cmd_queue)
    window.show()

    sys.exit(app.exec())
