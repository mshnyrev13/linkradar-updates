# Запуск из исходников и сборка

Приложение запускается командой `python -m linkradar`. Номер версии задаётся в `linkradar/__init__.py`.

| Папка | Назначение |
| --- | --- |
| `linkradar/ui/` | Окна и виджеты Qt |
| `linkradar/core/` | Хранение, статистика и отчёты |
| `linkradar/network/` | Сетевые пробы, захват и тест скорости |
| `linkradar/stream/` | OBS, vMix и NDI |
| `linkradar/system/` | Интеграция с ОС и фоновый движок |
| `packaging/` | Скрипты сборки и компоненты для Windows и macOS |
| `tests/`, `docs/` | Проверки и документация |

Прежний веб-интерфейс использует `static/`. Для запуска из исходников выполните `python -m linkradar.core.server`.

## Запуск

Для разработки используется Python 3.13.

macOS:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-desktop.txt
.venv/bin/python -m linkradar
```

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe -m linkradar
```

## Windows x64

В Actions задание **Build and verify Windows installer** загружает проверенный по SHA-256 Windows runtime с python.org, ставит закреплённые зависимости из PyPI, компилирует лаунчер из C и собирает Inno Setup. Проверка включает установку, запуск нативного окна с измерением loopback, повторную установку и удаление во временной папке. Задание загружает `Setup.exe` как отдельный артефакт. SHA-256 выводится в журнале сборки. Комплект содержит приложение, библиотеки и утилиты фонового движка. Инструкции доступны на GitHub.

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
