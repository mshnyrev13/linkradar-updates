"""Install the macOS engine and update stale code before connecting the GUI."""
import os
import json
from pathlib import Path
import subprocess
import sys
import time
import tempfile

import background_engine
import server

APP_BINARY = Path('/Applications/LinkRadar.app/Contents/MacOS/LinkRadar')


def pending_update(folder):
    path = Path(folder) / 'engine-update.json'
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_size > 2048:raise ValueError('Некорректное состояние обновления службы')
    value = json.loads(path.read_text())
    if (not isinstance(value,dict) or not isinstance(value.get('sid'),str)
            or not 0 < len(value['sid']) <= 128 or not isinstance(value.get('paused'),bool)
            or not isinstance(value.get('created'),(float,int))):
        raise ValueError('Некорректное состояние обновления службы')
    # Never resume a session from a much older installation after retention ended.
    if not 0 <= time.time() - value['created'] <= server.RETENTION_SECONDS:
        path.unlink();return None
    return value


def remember_update(folder, sid, paused):
    value = {'sid':sid, 'paused':bool(paused), 'created':time.time()}
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=folder,prefix='.engine-update-',delete=False) as f:
        temporary=Path(f.name)
        try:
            json.dump(value,f);f.flush();os.fsync(f.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True);raise
    try:os.replace(temporary,Path(folder)/'engine-update.json')
    finally:temporary.unlink(missing_ok=True)
    return value


def restore_update(folder, service, pending):
    if not pending:return
    if service.sid != pending['sid']:
        if service.sid:raise RuntimeError('Служба уже ведёт другую сессию; автоматическое восстановление отменено.')
        service.resume(pending['sid'], paused=pending['paused'])
    (Path(folder)/'engine-update.json').unlink(missing_ok=True)


def current(service):
    return (background_engine.current(service.status)
            and service.status.get('executable') == str(APP_BINARY))


def prepare(folder):
    """Return (status, explanation) before opening any monitoring database.

    Smoke/development/custom profiles never modify system jobs. A live service
    must run this version from the installed app, not merely speak the same IPC.
    Updating uses the normal macOS authorization dialog and keeps session data.
    """
    if (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)
            or '--smoke-test' in sys.argv or '--data-dir' in sys.argv):
        return 'skipped', ''
    service = background_engine.connect(folder)
    try:
        pending = pending_update(folder)
        if service and current(service):
            restore_update(folder,service,pending)
            return 'ready', ''
    except (OSError, ValueError, RuntimeError) as exc:
        return 'unavailable', 'Не удалось восстановить обновление: '+str(exc)+'. Данные сохранены.'
    if service:
        try:
            if tuple(map(int, service.status['version'].split('.'))) > tuple(map(int, server.VERSION.split('.'))):
                return 'unavailable', 'Фоновый движок новее этого окна LinkRadar. Откройте актуальное приложение из Applications; автоматическое понижение версии отменено.'
        except (KeyError, ValueError, AttributeError):
            pass
    if Path(sys.executable).resolve() != APP_BINARY.resolve():
        if service:return 'unavailable', background_engine.version_error(service.status)
        return 'skipped', 'Для автоматической установки фоновой службы перенесите приложение в Applications.'
    installer = Path(__file__).resolve().parent / 'packaging/install-background-mac.command'
    plist = Path('/Library/LaunchDaemons') / f'tech.vcs.network-watch.engine.{os.getuid()}.plist'
    upgrading = service is not None or plist.exists()
    if service and not plist.exists():
        return 'unavailable', 'Обнаружен старый движок без системной настройки запуска. Остановите его перед обновлением; второй движок не запущен.'
    previous = service.status if service else {}
    try:
        if previous.get('active_id'):
            if pending and pending['sid'] != previous['active_id']:
                raise RuntimeError('Сессия изменилась во время обновления; состояние восстановления сохранено.')
            if not pending:pending=remember_update(folder,previous['active_id'],previous.get('paused',False))
        args = ['/bin/sh', str(installer), '--gui'] + (['--upgrade'] if upgrading else [])
        # The authorization dialog may remain open until the user responds.
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode:
            if upgrading or plist.exists():
                return 'unavailable', 'Обновление фонового движка не завершено. Данные сохранены. Откройте LinkRadar снова и подтвердите системный запрос обновления службы.'
            return 'foreground', 'Установка фоновой службы отменена или не завершена. Сейчас работает обычный режим; при следующем открытии можно повторить установку.'
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            service = background_engine.connect(folder)
            if service and current(service):
                restore_update(folder,service,pending)
                return 'ready', ''
            time.sleep(.2)
        return 'unavailable', 'Актуальная фоновая служба не ответила после установки. Данные сохранены; второй движок не запущен.'
    except (OSError, RuntimeError, TimeoutError) as exc:
        if upgrading or plist.exists():
            return 'unavailable', 'Не удалось подключить обновлённую службу: '+str(exc)+'. Данные сохранены; второй движок не запущен.'
        return 'foreground', 'Не удалось запустить установщик фоновой службы. Приложение работает в обычном режиме.'
