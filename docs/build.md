# Запуск из исходников и сборка

`desktop.py` запускает настольный интерфейс Qt/PySide6, `server.py` содержит движок измерений и номер версии. Прежний веб-интерфейс находится в `static/`. [Документация веб-версии](archive/guides/README-WEB.md)

## Запуск

Для разработки используется Python 3.13.

macOS:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-desktop.txt
.venv/bin/python desktop.py
```

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe desktop.py
```

## Windows x64

В Actions задание **Build and verify Windows installer** загружает проверенный по SHA-256 Windows runtime с python.org, ставит закреплённые зависимости из PyPI, компилирует лаунчер из C и собирает Inno Setup. Проверка включает установку, запуск нативного окна с измерением loopback, повторную установку и удаление во временной папке. Задание загружает `Setup.exe` как отдельный артефакт. SHA-256 выводится в журнале сборки.

Для локальной сборки откройте x64 Native Tools Command Prompt for Visual Studio с MSVC и Windows SDK. Установите Python 3.13 и Inno Setup 6.4 или новее. В PowerShell из этого окружения выполните:

```powershell
py -3.13 packaging/build_windows.py --iscc "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
```

Результат находится в `dist/windows/`. Windows-сборка создаётся на Windows. Npcap и фоновый движок настраиваются отдельно по [инструкции для Windows](windows.md).

## macOS arm64

На Apple Silicon с Xcode Command Line Tools установите зависимости, затем выполните:

```sh
.venv/bin/python -m pip install pyinstaller==6.22.3
.venv/bin/python packaging/build-macos.py --output dist/macos --dmg
```

Скрипт создаёт DMG и подписывает приложение ad hoc. Для изолированной проверки запуска через LaunchServices:

```sh
open -W -n -a "$PWD/dist/macos/dist/LinkRadar.app" --args --smoke-test --no-discovery --smoke-report /private/tmp/linkradar-smoke.json
```

## Проверки

В активированной виртуальной среде:

```sh
python -m unittest discover -s tests -p 'test_*.py'
python tests/integration_local.py
node tests/test_start_stop.js
node tests/test_stream_analysis.js
```

Для Qt на Windows используется `QT_QPA_PLATFORM=windows`, на macOS в CI используется `offscreen`. Для проверок используйте отдельный временный профиль. Исторические отчёты с деталями отдельных сценариев находятся в [архиве](archive/README.md).
