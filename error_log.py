"""Local technical error journal: unexpected exceptions with tracebacks, no measurements.

Policy (privacy): one file per process kind in the data folder, at most 512 KB plus one
rotated copy; entries older than 12 hours are removed; URLs lose credentials, paths and
query, and stream keys, passwords, tokens and SRT parameters are masked before writing.
Unlike measurements, the journal survives a normal close so a failure stays diagnosable.
"""
import faulthandler
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

RETENTION_SECONDS = 12 * 3600
MAX_BYTES = 512 * 1024
CRASH_MAX_BYTES = 256 * 1024
PRUNE_SECONDS = 600
STAMP = '%Y-%m-%d %H:%M:%S'
logger = logging.getLogger('vcs')
# Without setup() (tests, imports) nothing is printed or written.
logger.addHandler(logging.NullHandler())

URL = re.compile(r'\b([a-z][a-z0-9+.-]{1,15})://([^\s\'"<>()]+)', re.I)
# Also matches dict/JSON reprs such as 'password': "value".
SECRET = re.compile(r'(?i)\b(key|stream_?key|password|passwd|pass|pwd|token|secret|streamid|passphrase|auth|authorization|cookie|api_?key)([\'"]?\s*[=:]\s*)("[^"]*"|\'[^\']*\'|[^\s,;&\'"}]+)')
TWITCH = re.compile(r'\blive_[A-Za-z0-9_]{8,}')
BEARER = re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}')


def scrub_url(match):
    scheme, rest = match.group(1), match.group(2)
    rest = rest.split('#', 1)[0].split('?', 1)[0]
    authority = rest.partition('/')[0]
    authority = authority.rsplit('@', 1)[-1]
    # RTMP keys may occupy the first path segment; retain only the endpoint.
    return scheme + '://' + authority


def scrub(text):
    """Mask secrets in free text. Hosts and IP addresses stay for diagnostics."""
    text = URL.sub(scrub_url, text)
    text = BEARER.sub(lambda m: m.group(1) + ' <скрыто>', text)
    text = SECRET.sub(lambda m: m.group(1) + m.group(2) + '<скрыто>', text)
    return TWITCH.sub('live_<скрыто>', text)


class Formatter(logging.Formatter):
    def format(self, record):
        return scrub(super().format(record))


def entry_time(line):
    try:
        return time.mktime(time.strptime(line[:19], STAMP))
    except ValueError:
        return None


def prune_file(path, now=None):
    """Drop entries (a stamped line and its traceback lines) older than the retention."""
    path = Path(path)
    if not path.is_file():
        return
    cutoff = (now or time.time()) - RETENTION_SECONDS
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines(keepends=True)
    except OSError:
        return
    kept = []
    keep = False
    for line in lines:
        stamp = entry_time(line)
        if stamp is not None:
            keep = stamp >= cutoff
        if keep:
            kept.append(line)
    if len(kept) != len(lines):
        if kept:
            path.write_text(''.join(kept), encoding='utf-8')
        else:
            path.unlink()


class Handler(logging.handlers.RotatingFileHandler):
    """Rotating file that also forgets entries older than 12 hours."""
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.pruned = 0
        super().__init__(path, maxBytes=MAX_BYTES, backupCount=1, encoding='utf-8', delay=True)
        self.prune()

    def prune(self):
        self.acquire()
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
            for path in (self.baseFilename, self.baseFilename + '.1'):
                prune_file(path, self.clock())
            self.pruned = self.clock()
        finally:
            self.release()

    def emit(self, record):
        if self.clock() - self.pruned >= PRUNE_SECONDS:
            self.prune()
        new = not os.path.exists(self.baseFilename)
        super().emit(record)
        if new and os.name != 'nt':
            try:
                os.chmod(self.baseFilename, 0o600)
            except OSError:
                pass


state = {'handler': None, 'crash': None, 'hooks': None}
last = {}


def setup(folder, kind='app'):
    """Install the journal and global exception hooks once per process."""
    if state['handler']:
        return Path(state['handler'].baseFilename)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    handler = Handler(folder / ('errors.log' if kind == 'app' else 'errors-' + kind + '.log'))
    handler.setFormatter(Formatter('%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s', STAMP))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    state['handler'] = handler
    state['hooks'] = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    sys.unraisablehook = unraisablehook
    crash = folder / ('crash.log' if kind == 'app' else 'crash-' + kind + '.log')
    try:
        # Native crashes (Qt, ctypes) bypass Python hooks; faulthandler writes file/line only.
        if crash.is_file() and (time.time() - crash.stat().st_mtime > RETENTION_SECONDS or crash.stat().st_size > CRASH_MAX_BYTES):
            crash.unlink()
        state['crash'] = open(crash, 'a', encoding='utf-8')
        faulthandler.enable(state['crash'], all_threads=True)
    except OSError:
        state['crash'] = None
    logger.info('Журнал ошибок включён: %s %s, Python %s', kind, sys.platform, sys.version.split()[0])
    return Path(handler.baseFilename)


def shutdown():
    """Remove the journal and restore previous hooks (tests and process exit)."""
    handler = state['handler']
    if handler:
        logger.removeHandler(handler)
        handler.close()
    if state['hooks']:
        sys.excepthook, threading.excepthook, sys.unraisablehook = state['hooks']
    if state['crash']:
        faulthandler.disable()
        state['crash'].close()
    state.update(handler=None, crash=None, hooks=None)
    last.clear()


def path():
    return Path(state['handler'].baseFilename) if state['handler'] else None


def echo(kind, value, tb):
    """Scrubbed copy for a console or launchd stderr file; nothing when there is no stderr."""
    if sys.stderr is not None:
        try:
            sys.stderr.write(scrub(''.join(traceback.format_exception(kind, value, tb))))
        except (OSError, ValueError):
            pass


def excepthook(kind, value, tb):
    logger.critical('Необработанное исключение', exc_info=(kind, value, tb))
    echo(kind, value, tb)


def thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else '?'
    logger.error('Необработанное исключение в потоке %s', name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    echo(args.exc_type, args.exc_value, args.exc_traceback)


def unraisablehook(info):
    logger.warning('Неперехватываемое исключение: %s', info.err_msg or repr(info.object),
                   exc_info=(info.exc_type, info.exc_value, info.exc_traceback) if info.exc_type else None)


def error(message, exc=None, key=None):
    """Log an error with its traceback; with key, only when the text changes (polling loops)."""
    text = message + (': %s: %s' % (type(exc).__name__, exc) if exc is not None else '')
    if key is not None:
        if last.get(key) == text:
            return
        last[key] = text
    logger.error(message, exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None)


def resolved(key):
    """Forget the last error of a polling loop so a repeat is logged again."""
    last.pop(key, None)
