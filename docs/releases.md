# Публикация и обновления LinkRadar

## Выпуск новой версии

1. Задайте версию `X.Y.Z` в `linkradar/__init__.py` и проверьте изменения в Actions.
2. Скачайте [Windows Setup.exe](build.md#windows-x64) и [macOS DMG](build.md#macos-arm64) из успешного прогона **Validate source** для этого коммита. Сверьте SHA-256 пакетов с дайджестами артефактов Actions.
3. Создайте выпуск с тегом `vX.Y.Z` для проверенного коммита. В описании укажите изменения и ссылки на загрузки. Добавьте файлы:
   - `LinkRadar-X.Y.Z-Windows-x64-Setup.exe`
   - `LinkRadar-X.Y.Z-macOS-arm64.dmg`
   - `SHA256SUMS.txt` с суммами файлов
4. Опубликуйте выпуск. GitHub добавляет ссылки на исходники тега автоматически.
5. Проверьте скачивание и [latest.json](https://raw.githubusercontent.com/mshnyrev13/linkradar-updates/main/latest.json). После публикации стабильного выпуска workflow **Update public version feed** проверяет наличие пакетов и обновляет канал. При необходимости его можно запустить вручную через Actions.

Канал принимает Windows ZIP в старых выпусках. Он пропускает черновики и prerelease и сверяет, что версия в канале не уменьшается.

## Проверки релиза 2.10.14

- Пакеты Windows x64 и macOS arm64 опубликованы 25 ноября 2026. Контрольные суммы обоих опубликованных файлов сверены с артефактами Actions. [Релиз](https://github.com/mshnyrev13/linkradar-updates/releases/tag/v2.10.14) · [SHA256SUMS.txt](https://github.com/mshnyrev13/linkradar-updates/releases/download/v2.10.14/SHA256SUMS.txt)
- Для коммита тега прошли тесты на обеих ОС, установка, запуск нативного окна, повторная установка и удаление на Windows, сборка DMG и пробный запуск на macOS. [Сборка и тесты](https://github.com/mshnyrev13/linkradar-updates/actions/runs/45626999504)
- Публичный канал возвращает версию 2.10.14 и ссылку на её страницу выпуска. [Прогон обновления канала](https://github.com/mshnyrev13/linkradar-updates/actions/runs/45628350131) · [latest.json](https://raw.githubusercontent.com/mshnyrev13/linkradar-updates/main/latest.json)

## Как работает проверка обновлений

Приложение проверяет `latest.json` при запуске и каждые 6 часов. Ручная проверка доступна в меню шестерёнки и окне «О приложении». «Скачать» открывает страницу выпуска. Установка выполняется вручную после закрытия старой версии.

Канал сохраняет прежний адрес и схему с `version` и `download_url`. HTTPS проверяется через системное хранилище доверия и certifi. Поддерживаются ссылки на Releases проекта и публичную страницу Яндекс Диска. Источник обновлений использует маршрут ОС.
