# LinkRadar для macOS

## Установка и обновление

1. Скачайте `LinkRadar-<версия>-macOS-arm64.dmg` из [последнего выпуска](https://github.com/mshnyrev13/linkradar-updates/releases/latest). Нужен Mac с Apple Silicon и macOS 15 или новее.
2. Откройте DMG и перенесите LinkRadar в «Программы». При обновлении сначала сохраните нужный TXT и закройте старую версию.
3. Запустите LinkRadar из «Программ». У сборки подпись ad hoc, без Developer ID и нотарификации. macOS может потребовать разрешение в «Конфиденциальность и безопасность».

## Сеть и захват

Разрешите LinkRadar доступ к локальной сети в системных настройках, если macOS покажет запрос. Для LLDP/CDP и захвата пакетов нужен доступ к BPF. При необходимости приложение предлагает настройку компонента доступа с правами администратора. Wireshark для этого не нужен.

Фоновый движок настраивается отдельно. После замены приложения с уже установленным движком перезагрузите Mac, чтобы служба загрузила новую версию.

[Функции и ограничения](https://github.com/mshnyrev13/linkradar-updates/blob/main/docs/standalone.md) · [Мониторинг OBS и vMix](https://github.com/mshnyrev13/linkradar-updates/blob/main/docs/stream.md)
