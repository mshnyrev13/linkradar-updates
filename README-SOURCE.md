# LinkRadar — исходники 2.10.9

Основное приложение — нативное окно Qt/PySide6 (`desktop.py`). HTTP-сервер и браузер для него не нужны. [README.md](README.md) содержит актуальные инструкции. [README-WEB.md](README-WEB.md), `static/` и `server.py` также описывают и содержат прежний веб-интерфейс; `server.py` используется нативным приложением как движок измерений.

## Запуск исходников

Нужен Python 3.12 или 3.13 (на Windows исходники ветки `feature/stream-tab` проверены и на 3.11.4). Готовые DMG/Windows ZIP уже содержат Python; для работы с исходниками установите его отдельно.

macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-desktop.txt
.venv/bin/python desktop.py
```

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe desktop.py
```

На Windows окно получает собственный значок и группу на панели задач (AppUserModelID `VCS.NetworkWatch.Desktop`), даже при запуске через `python.exe`. Для ярлыка на рабочем столе или панели задач укажите `.venv\Scripts\pythonw.exe desktop.py` и значок `branding\NetworkWatch.ico`.

`STANDALONE.md` описывает текущие функции, права LLDP, Graphene и ограничения измерений. Для Graphene нужна корпоративная сеть/VPN и доступ к источнику. Токены, cookies и пользовательские измерения в архив не включены.

Выбранный вручную адаптер используется для ping и ICMP-маршрута. Смена адаптера переключает новые пробы без сброса истории и сохраняет паузу; запоздалые ответы предыдущего адаптера отбрасываются. При отключении выбранного адаптера показывается недоступность, перехода на Wi-Fi или другой маршрут нет. «Автоматически» использует маршрут ОС. Для ping и маршрута LLDP не нужен. HTTP-тест скорости привязан к выбранному адаптеру; в Auto используется маршрут ОС. DNS использует резолвер ОС.

## Проверка

Результаты текущей стабилизации — [STABILITY-WORKLOG.md](STABILITY-WORKLOG.md).
Новый нативный read model не пересчитывает все графики hops при каждом такте.
Проверки большой истории воспроизводятся скриптами `audits/2026-10-21/` только
на синтетических профилях с marker-файлом. Они не используют установленную app.

Для приёмки Mac-сборки через LaunchServices, без установки службы/BPF и без
профиля пользователя:

```sh
open -W -n -a /absolute/build/dist/LinkRadar.app --args --smoke-test --no-discovery --smoke-interface en0 --smoke-target 192.0.2.1 --smoke-scenario --smoke-report /private/tmp/linkradar-acceptance.json
```

Подставьте адаптер и шлюз своей тестовой сети. Сценарий проверяет паузу,
продолжение, en999 без fallback, возврат адаптера, смену сервера, масштаб,
асинхронный TXT и очистку; данные сохраняются только во временном профиле.
`--smoke-scenario` требует `--smoke-report`. Такие флаги не предназначены для
обычного пользовательского запуска. Подпись и права OS этим тестом не меняются.

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

Команду выполняйте Python из созданной виртуальной среды. На Windows запускайте с `QT_QPA_PLATFORM=windows` (в PowerShell: `$env:QT_QPA_PLATFORM='windows'`): с `offscreen` два геометрических теста падают из-за шрифта-заглушки и экрана 800×800. Версия 2.9.0 (ветка `feature/stream-tab`): 175 тестов OK на Windows 10 и Python 3.11.4 (5 пропусков: 2 POSIX-only, 2 без TShark, 1 — символьные ссылки без прав), включая `tests/test_error_log.py` (журнал ошибок: маскировка секретов, 12 часов, ротация, глобальные обработчики), `tests/test_worker_resilience.py` (непредвиденные ошибки не останавливают потоки измерений, перезапуск потока) и `tests/test_stream.py` — фейковый obs-websocket, разбор vMix XML/Statistics, чтение чужого WinForms ListView, mDNS, агрегатор, отчёт без ключей и Qt-вкладку. На Mac прошли 113 тестов версии 2.8.1, включая привязку исходного ICMP-сокета/адреса, смену адаптера в GUI и через IPC, отбрасывание запоздалых ping/hop-ответов, скользящее хранение 12 часов, очистку базы и логов при закрытии через локальный и фоновый движки, защиту от запоздалых результатов, таблицу пакетов на пять строк, прогресс внутри кнопки и отмену теста скорости, компоновку на двух размерах окна, замер по 15 секунд в обоих направлениях, отключение/возврат USB с сохранением выбора, защиту от LLDP другого адаптера и гонки завершения захвата, вертикальное растягивание, тёмный фон строк, контраст кнопок, паузу с двухминутной историей, фоновые IPC-сессии, экспорт и прокрутку. Нативные графики Graphene проверены в упакованной Mac-сборке; выполнение Windows-сборки пока не проверено.

Нативный и фоновый движки используют `Store(..., retention_seconds=12 * 3600)`. Обычный выход вызывает `Monitor.clear()` либо аутентифицированную IPC-команду `clear`: остановка рабочих потоков, очистка таблиц, VACUUM и усечение WAL/служебных логов. При ошибке очистки окно не закрывается и показывает сообщение. Прежний веб-режим имеет отдельную политику хранения и не предназначен для проверки поведения настольной версии.

## Сборка автономного приложения

Установите PyInstaller в виртуальную среду и собирайте отдельно на каждой целевой ОС:

```sh
python -m pip install pyinstaller==6.22.3
python -m PyInstaller --noconfirm --windowed --onedir --name "LinkRadar" --add-data "branding:branding" --add-data "packaging:packaging" --collect-data certifi --copy-metadata truststore desktop.py
```

Это базовая сборка. Файлы значков находятся в `branding/`, исходник Windows GUI launcher — `packaging/windows_launcher.c`. Установочный DMG, подпись приложения и переносимый Windows runtime требуют дополнительной упаковки. Сборка macOS не создаёт Windows EXE.

### Windows portable ZIP

```powershell
python packaging\build_windows_zip.py --base LinkRadar-2.10.0-Windows-x64.zip --certifi-wheel certifi-2026.10.19-py3-none-any.whl --out ..\dist
```

Скрипт берёт из предыдущего ZIP `runtime` (embedded Python 3.13.12 x64), `packages` (зависимости `requirements-desktop.txt`) и `LinkRadar.exe` (`packaging/windows_launcher.c`), а `app` (все модули из корня и `branding`), скрипты службы и документацию — из рабочей копии. При изменении зависимостей или лаунчера базовый runtime нужно пересобрать отдельно. Печатает имя и SHA-256 архива.

Фоновый движок запускается через `desktop.py --engine-service --data-dir /path/to/data` без Qt-окна; задача SYSTEM на Windows — `desktop.py --engine-service --user-sid <SID>` (папка данных в ProgramData выводится из SID, `service_paths.py`). Скрипты установки/удаления системного фонового режима находятся в `packaging/`; подробности — STANDALONE.md.

### Воспроизводимая Mac-сборка 2.8.2

После установки зависимостей и PyInstaller выполните на Apple Silicon:

```sh
python packaging/build-macos.py --output /private/tmp/network-watch-macos-build --dmg
```

Скрипт включает данные, значок, metadata зависимостей и скрипты настройки BPF, задаёт версию Info.plist, macOS 15+ и подпись ad hoc, затем проверяет её. Он не устанавливает приложение или системные службы. `dist/LinkRadar.app` внутри указанного каталога готово для упаковки в DMG.

В DMG находятся только `LinkRadar.app` и ссылка на `Applications`. Скрипты установки/удаления и `bpf-access-root.sh` уже внутри `.app`, в `Contents/Resources/packaging`; автозапуск использует их оттуда. Инструкции `README.md`, `STANDALONE.md` и `STREAM.md` включаются в `Contents/Resources/Documentation` до подписи. Флаг `--dmg` собирает образ с этим составом, без отдельных скриптов и документов на диске.

`mac_capture.prepare()` не запрашивает права при запуске исходников, `--smoke-test`, `--data-dir` или `--engine-service`. Тесты установщика работают с временной файловой системой и подменёнными системными командами, без реального sudo, изменения групп, launchd или захвата. Это не замена проверке чистого Mac.


### Проверка ручного адаптера в Mac GUI

Начиная с 2.9.2, для сборки нужны Xcode Command Line Tools: build-macos.py компилирует
packaging/mac-network-access.swift в Contents/MacOS/LinkRadarNetworkAccess до подписи.
Помощник использует системный Network.framework, ждёт готовности UDP-соединения
на конкретном NWInterface и исходном адресе и не вызывает send(). Отдельный Swift
или Python пользователю готового приложения не нужен.

Временно изолированное окно через Finder/LaunchServices проверяют так:

```sh
open -W -n -a /path/to/LinkRadar.app --args --smoke-test --no-discovery \
  --smoke-interface en0 --smoke-target 192.0.2.1 --smoke-seconds 10 \
  --smoke-report /private/tmp/linkradar-probe.json
```

Укажите существующий интерфейс и разрешённую цель своей тестовой сети вместо
учебного IP. Дополнительные параметры работают только вместе с --smoke-test;
профиль временный, автоустановка компонентов отключена. JSON/скриншот могут
содержать реальные измерения — не включайте их в Git. Проверка из терминала
без LaunchServices не подтверждает работу Finder-приложения.

Проверка безопасности 2.10.1: см. [SECURITY-AUDIT.md](SECURITY-AUDIT.md). Обновлена certifi и усилены установщики/IPC; запуск Windows остаётся отдельной приёмкой.

Проверка скорости 2.10.2: [SPEED-AUDIT.md](SPEED-AUDIT.md). Уменьшен стартовый upload-блок для медленных каналов; в экспорте сохраняются измеренные байты и длительности. Скорость — HTTP goodput до CDN, а не паспортная скорость порта.

Визуальный/кодовый аудит 2.10.3: [AUDIT-2.10.3.md](AUDIT-2.10.3.md). Исправлены заполнение длинных графиков и шкалы; перерисовка использует ограниченный кэш, точные hover-данные сохранены.

Аудит точности 2.10.4: [AUDIT-2.10.4.md](AUDIT-2.10.4.md). Графики сохраняют extrema RTT/jitter и последнюю пробу; Stream считает скорость потерь по фактическому интервалу.

Циклы стабилизации 2.10.5: [AUDIT-2.10.5.md](AUDIT-2.10.5.md). Запись сессии вынесена из GUI, исправлены навигация/журналы, ускорена таблица пакетов.


Проверка обновлений с 2.10.6: при запуске и каждые 6 часов; вручную — «О приложении», «Справка» или шестерёнка. «Скачать» открывает страницу выпуска, установка вручную. Подробности и доступ к скачиванию — [UPDATES.md](UPDATES.md).

Аудит 2.10.8: [AUDIT-2.10.8.md](AUDIT-2.10.8.md). Ускорены длинные SQL-агрегаты и тихий журнал; плотные флапы Stream ограничены пикселями без потери исходной истории.


Аудит 2.10.9 — [AUDIT-2.10.9.md](AUDIT-2.10.9.md): согласованный Stream TXT,
фоновые copy/save, общий кэш журналов, ограниченные пропуски Graphene.
В изолированном Finder-сценарии добавьте `--smoke-scenario --smoke-stream-report`:
после сетевых этапов проверяются 5000 учебных Stream-событий и асинхронный TXT.
Это тест UI/отчёта, не проверка живого OBS/vMix; сеть в начальных этапах реальная.
