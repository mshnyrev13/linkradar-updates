"""Application identity and the user-opened About window."""
import html
import platform
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QUrl, qVersion, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QTabWidget, QWidget, QPushButton

from server import VERSION

from app_identity import NAME, PROJECT_URL, GUIDE_URL
ROOT = Path(__file__).resolve().parent


def documentation_path():
    if getattr(sys, 'frozen', False) and sys.platform == 'darwin':
        return Path(sys.executable).parents[1] / 'Resources/Documentation/README.md'
    for path in (ROOT / 'README.md', ROOT.parent / 'README.md'):
        if path.is_file():
            return path
    return None


class AboutDialog(QDialog):
    update_requested=Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('О приложении')
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(12)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        about = QWidget()
        about_layout = QVBoxLayout(about)
        about_layout.setContentsMargins(16, 16, 16, 16)
        about_layout.setSpacing(12)
        self.tabs.addTab(about, 'О приложении')

        def label(text, style=''):
            widget = QLabel(text)
            widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            widget.setWordWrap(True)
            if style:
                widget.setStyleSheet(style)
            about_layout.addWidget(widget)
            return widget

        logo = label('')
        logo.setPixmap(QPixmap(str(ROOT / 'branding/network-watch-logo.png')).scaled(
            80, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        label(NAME, 'font-size:20px;font-weight:600;color:#dce6f4;')
        self.version_label = label('Версия ' + VERSION, 'color:#a9bad0;')
        self.update_button=QPushButton('Проверить обновления…');self.update_button.clicked.connect(self.update_requested.emit);about_layout.addWidget(self.update_button)
        label('Диагностика сети и мониторинг трансляций.')
        system = 'macOS ' + platform.mac_ver()[0] if sys.platform == 'darwin' else platform.system() + ' ' + platform.release()
        label(html.escape(system + ' · ' + platform.machine()) + '<br>Python ' + platform.python_version()
              + ' · Qt / PySide6 ' + qVersion(), 'font-size:11px;color:#a9bad0;')
        links = f'<a style="color:#7db5ff" href="{PROJECT_URL}">Проект на GitHub</a>'
        links += f' &nbsp; · &nbsp; <a style="color:#7db5ff" href="{GUIDE_URL}">Руководство на Wiki</a>'
        label(links).setOpenExternalLinks(True)
        label('Qt / PySide6 — LGPLv3. Лицензии компонентов входят в поставку.',
              'font-size:10px;color:#a9bad0;')
        guide = QWidget()
        guide_layout = QVBoxLayout(guide)
        guide_layout.setContentsMargins(20, 24, 20, 24)
        guide_layout.setSpacing(16)
        self.tabs.addTab(guide, 'Руководство')
        for text in ('LinkRadar — инструкция для техников',
                     'Быстрый запуск, чтение графиков и сохранение результатов — на картинках.',
                     f'<a style="color:#7db5ff" href="{GUIDE_URL}">Открыть руководство на Wiki →</a>'):
            item = QLabel(text)
            item.setWordWrap(True)
            item.setOpenExternalLinks(True)
            guide_layout.addWidget(item)
        local = documentation_path()
        if local is not None:
            item = QLabel('<a style="color:#7db5ff" href="' + html.escape(QUrl.fromLocalFile(str(local)).toString(), quote=True) + '">Инструкция из комплекта приложения</a>')
            item.setOpenExternalLinks(True)
            guide_layout.addWidget(item)
        guide_layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('Закрыть')
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
