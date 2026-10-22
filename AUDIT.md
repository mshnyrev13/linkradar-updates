# Аудит VCS Network Watch — 16.10.2026

Ветка `feature/stream-tab` (1653b74). Прочитаны все модули Python (~5 400 строк), установщики `packaging/`; веб-интерфейс `static/` — бегло (прежний режим). Проверено запуском: пункты 1 и 8. Остальное — по коду, помечено, если не воспроизводилось.

## Итог

- Логирования не было (на момент аудита; п. 2 исправлен): модуль `logging` не использовался, файла журнала у приложения не было. Ошибки показываются в интерфейсе (события сессии, статусы, QMessageBox) и часто сводятся к общему тексту или имени исключения. Трассировки стека никуда не попадают в собранном приложении.
- Главный риск — тихая остановка измерений: необработанное исключение завершает поток пинга/маршрута, а сессия остаётся «в эфире».
- Хардкод в основном осознанный (Яндекс, Graphene, пути ОС), но не вынесен в конфигурацию.

## Высокий приоритет

1. **Исправлено** (тот же день, `tests/test_worker_resilience.py`). **Поток измерений умирал молча** — [server.py:688](server.py:688) `worker`, [server.py:616](server.py:616) `route_worker`. Перехватываются только `OSError`/`SubprocessError` и ошибки внутри `probe()`. Любая другая ошибка (например, `sqlite3.OperationalError` при заполненном диске или «database is locked») завершает поток. Проверено: после `KeyError` на третьей пробе поток мёртв, сессия `running`, `sid` активен, в событиях только «Проверка запущена». В интерфейсе остаётся LIVE, графики просто останавливаются.
   Сделано: тело цикла в `try/except Exception` — одно событие на серию сбоев, повтор на следующем такте, событие о восстановлении; ошибка, вырвавшаяся из функции потока, записывается событием; `timer` каждые 5 с перезапускает досрочно завершившийся поток (до 3 раз).

2. **Исправлено** (тот же день, `error_log.py`, `tests/test_error_log.py`; политика — STANDALONE.md). **Диагностика терялась**:
   - фоновый движок Windows запускается через `pythonw.exe` от SYSTEM ([install-background-windows.ps1:28](packaging/install-background-windows.ps1:28)) — `stderr` отсутствует, трассировки и `threading.excepthook` ничего не сохраняют;
   - собранное приложение (`--windowed`) — то же для GUI: исключения в слотах Qt и в `QThread.run` (например, `Discovery.run` без `try`) не видны; `sys.excepthook`/`threading.excepthook` не установлены;
   - macOS: `service-out.log`/`service-error.log` обнуляются при каждом закрытии окна ([server.py:395](server.py:395)) — после сбоя и перезапуска причины нет;
   - ошибки сводятся к общему тексту: [stream_monitor.py:89](stream_monitor.py:89) (только имя класса), [network_info.py:266](network_info.py:266), [network_info.py:279](network_info.py:279), [traffic_capture.py:176](traffic_capture.py:176), `SpeedWorker` ([desktop.py:67](desktop.py:67)), [stream_widgets.py:211](stream_widgets.py:211), код Windows ICMP теряется в `probe()` («Сетевая или локальная ошибка ОС»), `stderr` dumpcap читается и выбрасывается ([packet_tools.py:160](packet_tools.py:160)).

   Сделано: `error_log.py` — журнал по типу процесса (`errors.log`, `errors-service.log`, `errors-web.log`), 512 КБ + 1 копия, записи старше 12 ч удаляются, секреты маскируются до записи, журнал переживает обычное закрытие. `sys.excepthook`, `threading.excepthook`, `sys.unraisablehook`, `faulthandler` (`crash*.log`), Qt critical/fatal. Перечисленные места пишут в журнал; HTTP-обработчик веб-режима отвечает 500 и пишет трассировку (часть п. 7). Кнопка «Журнал ошибок» в окне.

3. **Исправлено** (тот же день, `service_paths.py`, `tests/test_service_paths.py`; служба вживую не устанавливалась). **Служба SYSTEM пишет в папку, которой полностью владеет пользователь** (Windows, не проверено на практике). Установщик даёт пользователю Full Control на `%LOCALAPPDATA%\VCS Network Watch`, служба от SYSTEM создаёт и усекает там файлы (`network.sqlite3`, `-wal`, `service.lock`, `service-*.log`). Пользователь может подменить папку junction-ссылкой, а файлы — жёсткими ссылками. Проверка `is_symlink()` не ловит hardlink и junction — классический вектор повышения прав (создание/усечение файлов от SYSTEM).
   Что сделать: данные службы хранить в `%ProgramData%` с ACL SYSTEM/админы и отдавать пользователю только через IPC. Как минимум — проверять reparse point у папки и количество ссылок у файлов перед записью. Фоновый режим Windows по MEMORY.md вживую не проверялся.
   Сделано:
   - Данные службы — `%ProgramData%\VCS Network Watch\<id>`, `<id>` — первые 16 hex SHA-256 от SID пользователя. Задача запускается как `--engine-service --user-sid <SID>`: путь к данным и SID задаёт только администратор (задание Task Scheduler), файлов `service-user.sid` и папки данных в аргументах больше нет.
   - Служба сама создаёт и запирает папки до открытия чего-либо: открывает их без следования reparse point (`FILE_FLAG_OPEN_REPARSE_POINT`), проверяет атрибуты и через тот же дескриптор ставит владельца SYSTEM и защищённый DACL. Корень: SYSTEM/админы — полный, Users — только просмотр. Папка пользователя: SYSTEM/админы — полный, пользователь — только просмотр (без наследования). Подложенный junction удаляется как ссылка, папка создаётся заново; повторная подмена — отказ запуска.
   - Перед открытием каждое имя службы (`network.sqlite3`, `-wal/-shm/-journal`, `service.lock`, `service.key`, `errors-service.log`(.1), `crash-service.log`) проверяется: reparse point, несколько жёстких ссылок, не обычный файл или владелец не SYSTEM/админы — удаляется только имя, цель не трогается. Если папкой раньше владел кто-то другой, удаляются все имена службы. `Store.clear` больше не усекает файл через hardlink/junction (была только проверка `is_symlink()`).
   - `service.key` создаётся заново при каждом запуске службы сразу с DACL (SYSTEM/админы, пользователь — чтение): подложенный заранее ключ не используется.
   - GUI на Windows не открывает базу службы: данные сессии (сеть, скорость, Graphene), события теста скорости и экспорт TXT идут через аутентифицированный канал (`record`, `event`, `export`; capability `session_data`). Служба принимает только виды данных GUI (не `engine_state`), существующие сессии и конечные метки времени. Своя база GUI в `%LOCALAPPDATA%` остаётся только для обычного режима.
   - Миграция: установщик снимает задачу и файлы прежней службы (имя по хешу папки пользователя), после успешной установки уже без прав администратора удаляет из `%LOCALAPPDATA%` её файлы (`service-user.sid`, ключ, блокировку, журналы, базу). Новое окно при работающей прежней службе предупреждает и не запускает второй движок. Скрипт удаления снимает новую и прежнюю задачи и удаляет папку данных в ProgramData (`rmdir /s`, который не заходит в junction).
   - Не проверено: реальная установка задачи SYSTEM, смена владельца на SYSTEM (тесты применяют тот же защищённый DACL с SID пользователя), подмена папки во время работы установщика, portable ZIP (`service_paths.py` должен попасть в `app\`). macOS не менялся: там служба работает от пользователя, границы привилегий нет; `StandardOutPath` launchd в папке пользователя не исследовался.

## Средний приоритет

4. **Запись в БД из GUI-потока**: `store.prune()` каждые 500 мс в `refresh` ([desktop.py:566](desktop.py:566)), `executemany` Graphene ([desktop.py:544](desktop.py:544)). При фоновой службе два процесса пишут в одну SQLite: возможны подвисания UI и «database is locked» (по умолчанию ожидание 5 с) — в GUI это исключение в слоте (см. п. 2), в службе — п. 1.
5. **Полный список событий в каждом снимке**: `event_limit=None` ([desktop.py:49](desktop.py:49), [background_engine.py:86](background_engine.py:86)). Каждые 0,5–2 с через IPC и JSON уходят все события сессии за 12 часов; при частых сбоях объём растёт линейно.
6. **Закрытие окна может зависнуть**: `discovery.wait()` и `speed_worker.wait()` без таймаута ([desktop.py:631](desktop.py:631)). Ограничено таймаутами PowerShell (6 с) и HTTP (4 с), но UI в это время не отвечает.
7. **Прежний веб-сервер** ([server.py:1014](server.py:1014), [server.py:1057](server.py:1057)) ловит только `ValueError`/`TypeError`. `sqlite3`-ошибки, `KeyError` в `csv_export`, `FileNotFoundError` рвут соединение без ответа; `log_message` отключён. Загруженные/захваченные PCAP в `data/captures` никогда не удаляются.

## Хардкод

| Где | Что | Замечание |
| --- | --- | --- |
| [graphene_port.py:12](graphene_port.py:12) | `https://graphene.yandex-team.ru`, project `noc`, table `network`, суффиксы `.yndx.net/.yandex.net/.yandex-team.ru` | корпоративный сервис; вынести в настройки |
| [speed_test.py:18](speed_test.py:18) | `yandex.ru/internet/api/v0/get-probes`, белый список `*.cdn.yandex.net` | единственный провайдер теста скорости |
| [speed_test.py:75](speed_test.py:75) | `User-Agent: VCS-Network-Watch/2.0` | версия не совпадает с `server.VERSION` 2.8.1 |
| [desktop.py:243](desktop.py:243) | серверы по умолчанию `src-ugc.strm.yandex.net`, `ya.ru`, `google.com` | — |
| [desktop.py:682](desktop.py:682) | `switch-a.example.test` / `Gi1/0/17` в smoke-тесте | внутреннее имя коммутатора в коде |
| [desktop.py:684](desktop.py:684) | `/private/tmp/...png` | путь macOS; на Windows скриншот не сохраняется |
| [desktop.py:648](desktop.py:648) | папка данных: не-Windows → `~/Library/Application Support` | на Linux неверно (Linux не поддерживается) |
| [packet_tools.py:20](packet_tools.py:20), [network_info.py:190](network_info.py:190) | `C:/Program Files/Wireshark`, `System32/Npcap`, `/usr/lib/libpcap.A.dylib` | стандартные пути; нестандартная установка не найдётся |
| [mac_background.py:10](mac_background.py:10) | `/Applications/VCS Network Watch.app` | автоустановка службы только из Applications (намеренно) |
| [server.py:33](server.py:33), [traffic_capture.py:13](traffic_capture.py:13) | 12 ч хранения, 120 с пакетов, 50 000 пакетов / 32 МБ | требования проекта (AGENTS.md) — не менять |
| [server.py:1063](server.py:1063), [stream_sources.py:23](stream_sources.py:23) | порт 8765, obs-websocket 4455, vMix `127.0.0.1:8088` | переопределяются аргументом/полем UI |
| [desktop.py:70](desktop.py:70), [desktop.py:127](desktop.py:127) | пороги цвета RTT 100/200, jitter 10/35 мс | дублируются в двух местах |

## Низкий приоритет

8. Lock-файлы растут на 1 байт при каждом запуске: `open('a+b')` + `write(b'0')` всегда дописывает в конец ([desktop.py:659](desktop.py:659), [background_engine.py:50](background_engine.py:50)). Проверено: 3 открытия → 3 байта. В `server.py` это уже учтено.
9. `--data-dir` последним аргументом без значения → `IndexError` ([desktop.py:650](desktop.py:650)).
10. Мёртвый код: `self.context`, `self.opener` в `SpeedTest` не используются ([speed_test.py:65](speed_test.py:65)).
11. Защита CSV от формул не учитывает ведущие `\t` и `\r` ([server.py:912](server.py:912)).
12. Ответ Graphene читается целиком до проверки размера 20 МБ ([graphene_port.py:128](graphene_port.py:128)).
13. Windows `network_info` запускает PowerShell каждые 10 с (заметная нагрузка CPU на слабых ПК).

## Что уже хорошо

- Ключи, пароли и параметры SRT вычищаются (`safe_url`, проверено на эфире).
- Валидация ввода: хосты, порты, интервалы, интерфейсы, размер запросов.
- Локальный HTTP: проверка Host/Origin, токен, CSP. IPC службы: ключ и ACL канала.
- Устойчивость к сбоям сети в Stream: медленные источники вынесены в отдельные потоки.

## Рекомендуемый порядок

1. ~~Перехват исключений в `worker`/`route_worker` + сторож живости потоков (п. 1)~~ — сделано.
2. ~~Журнал ошибок и глобальные обработчики исключений (п. 2)~~ — сделано.
3. ~~Перенос данных службы Windows из пользовательской папки (п. 3)~~ — сделано; до выпуска проверить установку на Windows вживую.
4. Вынести адреса Graphene/теста скорости в настройки; убрать `switch-a.example.test` из кода.
