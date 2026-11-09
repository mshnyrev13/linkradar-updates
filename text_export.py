"""Shared TXT save locations; never depend on Finder's working directory."""
from pathlib import Path
from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QFileDialog

DIRECTORY_PROPERTY = 'linkradarExportDirectory'


def initial_path(parent, filename):
    remembered = parent.window().property(DIRECTORY_PROPERTY)
    candidates = [remembered] if isinstance(remembered, str) else []
    candidates.extend(QStandardPaths.writableLocation(location) for location in (
        QStandardPaths.StandardLocation.DocumentsLocation,
        QStandardPaths.StandardLocation.DownloadLocation,
        QStandardPaths.StandardLocation.HomeLocation))
    candidates.append(str(Path.home()))
    for candidate in candidates:
        if not candidate:
            continue
        folder = Path(candidate)
        if folder.is_absolute() and folder != Path(folder.anchor) and folder.is_dir():
            return folder / filename
    return Path.home() / filename


def choose_path(parent, title, filename):
    initial = initial_path(parent, filename)
    path, _ = QFileDialog.getSaveFileName(parent, title, str(initial), 'Текст (*.txt)')
    if not path:
        return None
    target = Path(path)
    # Native dialogs may return only the entered name; Finder's cwd is often /.
    if not target.is_absolute():
        target = initial.parent / target
    return str(target)


def remember_path(parent, path):
    parent.window().setProperty(DIRECTORY_PROPERTY, str(Path(path).absolute().parent))
