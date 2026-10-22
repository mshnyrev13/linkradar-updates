"""Wait for the public asynchronous close contract before disposing a test DB."""
import time
from contextlib import contextmanager
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication

def production_application(test_case):
    """Use the same Fusion/theme bootstrap as desktop.main, restore after the class."""
    import desktop
    app=QApplication.instance() or QApplication([])
    old_style,old_sheet=app.style().objectName(),app.styleSheet()
    def restore():
        app.setStyle(old_style);app.setStyleSheet(old_sheet)
    test_case.addClassCleanup(restore)
    app.setStyle('Fusion');app.setStyleSheet(desktop.STYLE)
    return app

def settle_layout(app):
    # A resize can queue balancing, which queues another resize/layout pass.
    for _ in range(10):app.processEvents()

@contextmanager
def managed_window(window):
    """Dispose a GUI and its DB before TemporaryDirectory, including failed asserts."""
    try:
        yield window
    finally:
        try:
            close_window(window)
        finally:
            if window.store is not None:window.store.db.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

def close_window(window,seconds=8):
    window.close();deadline=time.monotonic()+seconds
    while not window.closed_clean and time.monotonic()<deadline:
        QApplication.processEvents();time.sleep(.005)
    assert window.closed_clean,'Window did not finish cleanup'

def flush_io(window):
    if window.io:assert window.io.barrier().wait(5),'I/O queue did not drain'
    QApplication.processEvents()

def flush_report(panel,seconds=5):
    deadline=time.monotonic()+seconds
    while panel.report_pending and time.monotonic()<deadline:
        QApplication.processEvents();time.sleep(.005)
    assert not panel.report_pending,'Stream report did not finish'
    QApplication.processEvents()
