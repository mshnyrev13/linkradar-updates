"""First-launch BPF access setup. Inspect permissions without opening a capture."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

APP_BINARY = Path('/Applications/LinkRadar.app/Contents/MacOS/LinkRadar')


def access_ready():
    for device in Path('/dev').glob('bpf[0-9]*'):
        try:
            if (not device.is_symlink() and stat.S_ISCHR(device.stat().st_mode)
                    and os.access(device, os.R_OK | os.W_OK)):
                return True
        except OSError:
            continue
    return False


def prepare():
    """Return status/message; never grant access in development or smoke tests."""
    if (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)
            or any(flag in sys.argv for flag in ('--smoke-test', '--data-dir', '--engine-service'))):
        return 'skipped', ''
    if access_ready():
        return 'ready', ''
    if Path(sys.executable).resolve() != APP_BINARY.resolve():
        return 'skipped', ('Для настройки LLDP и захвата перенесите приложение в Applications. '
                           'Wireshark не требуется.')
    installer = Path(__file__).resolve().parent / 'packaging/install-capture-access-mac.command'
    try:
        # Authorization belongs to macOS; no timeout while the user enters a password.
        result = subprocess.run(['/bin/sh', str(installer), '--gui'], capture_output=True, text=True)
    except OSError:
        return 'error', ('Не удалось запустить настройку BPF. LLDP и захват недоступны; '
                         'ping работает. Повторите запуск или используйте установщик доступа из DMG.')
    if result.returncode:
        return 'cancelled', ('Доступ BPF не настроен: подтверждение отменено или установка не завершена. '
                             'LLDP и захват недоступны; ping работает. При следующем открытии можно повторить настройку.')
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if access_ready():
            return 'ready', ''
        time.sleep(.2)
    return 'restart', ('Компонент доступа BPF установлен. Чтобы обновить права текущей учётной записи, '
                       'выйдите из неё и войдите снова либо перезагрузите Mac. Затем откройте приложение. '
                       'До этого LLDP и захват могут оставаться недоступны; ping работает.')
