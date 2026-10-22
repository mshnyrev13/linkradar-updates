# Источники и интеграция

- [Wireshark — официальный репозиторий](https://gitlab.com/wireshark/wireshark): прочитана документация о TShark и dumpcap. Интеграция запускает установленный TShark для разбора PCAP/PCAPNG и установленный dumpcap для добровольно запущенного пользователем захвата. Бинарники и исходники Wireshark не включены в архив приложения; оно не является сборкой Wireshark.
- [whitespring/pingplotter](https://github.com/whitespring/pingplotter): независимый проект на Node.js/PostgreSQL с браузерным интерфейсом, не исходный код коммерческого PingPlotter. Изучены README, документация монитора и подход к хранению истории узлов и временным фильтрам. Исходники не копировались; лицензия в просмотренных метаданных GitHub не заявлена.
- [PingPlotter — Summary Graphs](https://www.pingplotter.com/manual/summary_graphs/): опорная документация поведения графиков.
- Установленный коммерческий PingPlotter 5.27.13: только чтение данных активной диагностики ya.ru; настройки и лицензии не изменялись. Подтверждены IPv6, ICMP, семь узлов и отсутствие ответов двух промежуточных узлов. Коммерческий движок не перенесён: Network Watch использует системные последовательные трассировки.

Network Watch — собственная реализация нужных диагностических сценариев. Она не воспроизводит все функции PingPlotter или Wireshark.

Версия 1.2 использует присланный пользователем скриншот PingPlotter как образец компоновки таблицы и графиков. Фирменные логотипы, баннеры и название оригинального продукта в интерфейс не переносились; приложение подписано LinkRadar.


В версии 2.8.1 привязка ICMP проверена по официальным источникам: [Apple ping](https://github.com/apple-oss-distributions/network_cmds/blob/main/ping.tproj/ping.c), [Apple ping6](https://github.com/apple-oss-distributions/network_cmds/blob/main/ping6.tproj/ping6.c), [IcmpSendEcho2Ex](https://learn.microsoft.com/en-us/windows/win32/api/icmpapi/nf-icmpapi-icmpsendecho2ex) и [Icmp6SendEcho2](https://learn.microsoft.com/en-us/windows/win32/api/icmpapi/nf-icmpapi-icmp6sendecho2). Собственная реализация использует документированные сокеты/API; исходники Apple и Microsoft в комплект не включены.

В версии 2.8.2 изучен официальный [Wireshark ChmodBPF](https://github.com/wireshark/wireshark/tree/master/packaging/macosx/ChmodBPF): devfs не сохраняет права BPF после перезагрузки; доступ обычно предоставляется группе `access_bpf` системной задачей. Наш helper реализован отдельно, скрипты/бинарники Wireshark не копировались. В отличие от установки с группой admin добавляется только запрашивающий пользователь; root-задача исполняет собственный root-owned shell helper, а не GUI/Python из Applications.

Вкладка Stream (ветка feature/stream-tab):

- [obs-websocket protocol.md](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md): аутентификация, `GetStats`, `GetStreamStatus`, `GetRecordStatus`, `GetOutputList/Status`, `GetVideoSettings`, `GetStreamServiceSettings`. WebSocket-клиент собственный (RFC 6455), без зависимостей.
- VmixStatsMonitor (.NET 8 exe коллеги, исходники не получены): изучены строки файла — окно `Statistics`, класс `SysListView32`, `LVM_GETITEMTEXTW`, регулярные выражения аудио/Fullscreen/Connected. Код не копировался; чтение реализовано заново на ctypes.
- [vMix forum: health stats not in API XML](https://forums.vmix.com/posts/m120950-Feature-Request--Expose-Dropped-Frames-and-Health-Stats-in-API-XML--Stream--Record--SRT): XML API отдаёт только вкл/выкл.
- Microsoft: [GetExtendedTcpTable](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedtcptable), [GetPerTcpConnectionEStats](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getpertcpconnectionestats), [TCP_ESTATS_PATH_ROD_v0](https://learn.microsoft.com/en-us/windows/win32/api/tcpestats/ns-tcpestats-tcp_estats_path_rod_v0).
- Метрики SRT/потоков: [Haivision SRT statistics](https://doc.haivision.com/Kraken/4.3/srt-statistics); что передавать провайдеру: [Gcore — live streaming issues](https://gcore.com/docs/streaming/troubleshooting/live-streaming-issues), [Kaltura — stream health](https://knowledge.kaltura.com/help/stream-health-monitoring-using-the-kaltura-live-dashboard), [Jet-Stream — support](https://docs.jet-stream.com/docs/how-to/support/).
- NDI: обнаружение через mDNS `_ndi._tcp.local` (RFC 6762/6763), собственный разбор DNS; NDI SDK не используется.
