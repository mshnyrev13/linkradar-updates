# Проверки LinkRadar 2.10.10

[Публичная CI исходников](https://github.com/mshnyrev13/linkradar-updates/actions/runs/39751797812), код 59773c3c7099e8a711f2170292e1436f2f649b26.

| Проверка | Результат |
| --- | --- |
| Windows, native Qt | 391 unit / 64,329 с / 31 skipped / OK; 6 integration / 6,405 с / 1 skipped / OK |
| Windows, зависимости и JavaScript | pip-audit: уязвимостей не найдено; оба JS-набора OK |
| Локальный Mac, offscreen | 391 unit / 37,868 с / 12 skipped / OK; 6 integration / 4,609 с / OK; оба JS-набора OK |
| Mac app через Finder/LaunchServices | 2.10.10, LIVE, ya.ru 24/24 ответа, 0 потерь/локальных ошибок |
| Упаковка | macOS codesign и DMG checksum OK; Windows ZIP CRC OK; 57 исходных файлов обоих build manifest совпали; все Windows .py соответствуют исходникам |
| Публикация | SHA256 трёх архивов совпали с GitHub; скачивание без авторизации HTTP 200; канал 2.10.10; UpdateChecker предлагает обновление из 2.10.9 и не предлагает повторное для 2.10.10 |

macOS job публичной CI ожидала runner на момент фиксации; актуальное состояние по ссылке выше. Локальные Mac-проверки завершены. Пропуски платформенные/необязательные, в том числе TShark на Windows runner.

Исправлены фактическая минимальная высота Windows (900×640, справка через шестерёнку), тесты owner/DACL, освобождение Qt/SQLite, синхронизация snapshot/БД и TCP-фикстура. Продуктовые ACL, таймауты и расчёты не ослаблены.

Ограничения: выполнение portable EXE/Npcap/SYSTEM-службы и реального USB на Windows не проверено этим выпуском. macOS: Apple Silicon, 15+, подпись ad hoc без Developer ID/notarization. Loopback ICMP на тестовом Mac не ответил и приложению, и системному ping; публичная цель ответила обоим. Пользовательские настройки ОС не менялись.
