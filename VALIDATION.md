# Проверки LinkRadar 2.10.10

Исправления Windows CI проверены на исходном коде bfdbf7e2e475f91f85ebe2dc792bcdf132b3a56b:
https://github.com/mshnyrev13/vcs-network-watch/actions/runs/39749122346

- Windows: 391 unit, 31 skipped; 6 integration, 1 skipped; JS и pip-audit — success.
- macOS: 391 unit, 14 skipped; 6 integration, 1 skipped; JS и pip-audit — success.
- Исправлены тесты owner/DACL, синхронизация snapshot/БД и подготовка рабочего стола одноразовой Windows CI. Продуктовые ACL и расчёты не ослаблены.
- Исправлена реальная минимальная высота Windows: справка доступна через шестерёнку, окно 900×640.

Для публичного переноса обновлены версия и адрес проекта, внутренние имена в тестовых примерах заменены учебными. Новая CI запускается в этом репозитории.

Ограничения: Windows portable EXE/Npcap/SYSTEM-служба и реальный USB не проверялись этим выпуском на Windows; подпись macOS ad hoc, без notarization.
