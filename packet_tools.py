"""Wireshark integration through separately installed TShark/dumpcap. No cloud."""
import collections
import ipaddress
import json
import platform
import re
import shutil
import statistics
import subprocess
import threading
import time
import uuid
from pathlib import Path
import error_log


def tool(name):
    found = shutil.which(name)
    if found:
        return found
    candidates = [Path('/Applications/Wireshark.app/Contents/MacOS') / name,
                  Path('C:/Program Files/Wireshark') / (name + '.exe')]
    return next((str(p) for p in candidates if p.is_file()), None)


def process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if platform.system() == "Windows" else {}


class PacketTools:
    def __init__(self, root):
        self.root = Path(root) / "captures"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}

    def status(self):
        exe = tool('tshark')
        version = None
        if exe:
            try:
                p = subprocess.run([exe, '-v'], capture_output=True, timeout=5, **process_options())
                version = p.stdout.decode(errors='replace').splitlines()[0][:120]
            except (OSError, subprocess.SubprocessError, IndexError):
                version = 'TShark найден, но не удалось запустить'
        return {'available': bool(exe), 'version': version, 'capture_available': bool(tool('dumpcap'))}

    def interfaces(self):
        exe = tool('dumpcap')
        if not exe:
            return []
        try:
            p = subprocess.run([exe, '-D'], capture_output=True, timeout=10, **process_options())
        except (OSError, subprocess.SubprocessError):
            raise ValueError('Не удалось получить интерфейсы захвата Wireshark')
        if p.returncode:
            raise ValueError('Нет доступа к интерфейсам захвата. Проверьте штатную настройку Wireshark/Npcap.')
        rows = []
        for line in p.stdout.decode(errors='replace').splitlines():
            m = re.match(r'(\d+)\.\s+(.+)', line)
            if m:
                rows.append({'id': int(m.group(1)), 'label': m.group(2)[:180]})
        return rows

    def path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError('Некорректный ID файла')
        p = self.root / (identifier + '.pcapng')
        if not p.is_file():
            raise ValueError('Файл не найден')
        return p

    def upload(self, content):
        if not 24 <= len(content) <= 50 * 1024 * 1024:
            raise ValueError('Выберите PCAP/PCAPNG размером до 50 МБ')
        if content[:4] not in (b'\x0a\x0d\x0d\x0a', b'\xd4\xc3\xb2\xa1', b'\xa1\xb2\xc3\xd4', b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d'):
            raise ValueError('Файл не имеет заголовка PCAP/PCAPNG')
        identifier = uuid.uuid4().hex
        path = self.root / (identifier + '.pcapng')
        path.write_bytes(content)
        return identifier

    def analyze(self, identifier, display_filter=''):
        exe = tool('tshark')
        if not exe:
            raise ValueError('Установите Wireshark с TShark с официального сайта wireshark.org')
        path = self.path(identifier)
        display_filter = str(display_filter).strip()
        if len(display_filter) > 500:
            raise ValueError('Слишком длинный display filter')
        fields = ['frame.number','frame.time_epoch','frame.len','ip.src','ipv6.src','ip.dst','ipv6.dst',
                  '_ws.col.Protocol','tcp.analysis.retransmission','tcp.analysis.fast_retransmission',
                  'tcp.analysis.duplicate_ack','tcp.analysis.lost_segment','tcp.analysis.ack_rtt','tcp.flags.reset']
        command = [exe, '-n', '-r', str(path), '-c', '100000', '-T', 'fields', '-E', 'separator=/t', '-E', 'occurrence=f']
        if display_filter:
            command += ['-Y', display_filter]
        for field in fields:
            command += ['-e', field]
        try:
            p = subprocess.run(command, capture_output=True, timeout=40, **process_options())
        except (OSError, subprocess.SubprocessError):
            raise ValueError('TShark не завершил анализ за 40 секунд или не смог запуститься')
        if p.returncode:
            raise ValueError('TShark отклонил файл или display filter. Проверьте формат и синтаксис.')
        protocols = collections.Counter()
        conversations = collections.Counter()
        count = size = retrans = duplicates = lost = resets = 0
        first = last = None
        rtts = []
        frames = []
        for line in p.stdout.decode('utf-8',errors='replace').splitlines():
            cells = line.split('\t')
            if len(cells) < len(fields):
                cells += [''] * (len(fields)-len(cells))
            try:
                ts = float(cells[1]); length = int(cells[2])
            except ValueError:
                continue
            count += 1; size += length
            first = ts if first is None else min(first,ts); last = ts if last is None else max(last,ts)
            src = cells[3] or cells[4]; dst = cells[5] or cells[6]; protocol = cells[7] or 'Other'
            protocols[protocol] += 1
            conversations[(src,dst,protocol)] += 1
            flags = []
            if cells[8] or cells[9]: retrans += 1; flags.append('Retransmission')
            if cells[10]: duplicates += 1; flags.append('Duplicate ACK')
            if cells[11]: lost += 1; flags.append('Previous segment not captured')
            if cells[13].lower() in ('1', 'true'): resets += 1; flags.append('TCP RST')
            if cells[12]:
                try: rtts.append(float(cells[12])*1000)
                except ValueError: pass
            if len(frames) < 300:
                frames.append({'number': int(cells[0]), 'ts':ts,'length':length,'src':src,'dst':dst,'protocol':protocol,'flags':flags})
        duration = (last-first) if first is not None else 0
        return {'id':identifier,'filter':display_filter,'packets':count,'bytes':size,'duration':round(duration,3),
                'observed_mbps': round(size*8/duration/1e6,3) if duration else None,
                'tcp_retransmissions':retrans,'duplicate_acks':duplicates,'not_captured_segments':lost,'tcp_resets':resets,
                'tcp_ack_rtt_avg_ms':round(statistics.mean(rtts),3) if rtts else None,
                'protocols':dict(protocols.most_common()),'conversations':[{'src':a,'dst':b,'protocol':p,'packets':n} for (a,b,p),n in conversations.most_common(30)],
                'frames':frames,'file_bytes':path.stat().st_size,'limit':'Анализ первых 100 000 пакетов файла; таблица показывает первые 300 подходящих пакетов',
                'note':'Повторные передачи и пропущенные сегменты — сигналы TCP-анализа, а не измеренный процент сетевых потерь. Скорость относится к наблюдаемому трафику.'}

    def capture(self, interface, ip, seconds):
        exe = tool('dumpcap')
        if not exe:
            raise ValueError('Не найден dumpcap из Wireshark')
        ip = str(ipaddress.ip_address(ip))
        seconds = int(seconds)
        if not 5 <= seconds <= 60:
            raise ValueError('Длительность захвата: 5–60 секунд')
        if int(interface) not in [i['id'] for i in self.interfaces()]:
            raise ValueError('Выберите доступный интерфейс')
        with self.lock:
            if any(j['state']=='running' for j in self.jobs.values()):
                raise ValueError('Захват уже выполняется')
            identifier = uuid.uuid4().hex
            output = self.root / (identifier+'.pcapng')
            command = [exe, '-i', str(int(interface)), '-f', 'host '+ip, '-s', '128', '-a','duration:'+str(seconds),
                       '-a','filesize:32768','-w',str(output)]
            try:
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **process_options())
            except OSError:
                raise ValueError('Не удалось запустить dumpcap')
            self.jobs[identifier] = {'state':'running','id':identifier,'ip':ip,'seconds':seconds,'started':time.time(),'process':process}
            threading.Thread(target=self.capture_wait,args=(identifier,process,seconds),daemon=True).start()
            return identifier

    def capture_wait(self, identifier, process, seconds):
        try:
            _, stderr = process.communicate(timeout=seconds+10)
            state = 'complete' if process.returncode == 0 else 'error'
            if state == 'error':
                error_log.logger.error('dumpcap завершился с кодом %s: %s', process.returncode, (stderr or b'').decode(errors='replace')[-2000:])
        except subprocess.TimeoutExpired:
            process.kill(); process.communicate(); state='error'
        with self.lock:
            self.jobs[identifier]['state'] = state
            self.jobs[identifier]['message'] = 'Захват завершён' if state=='complete' else 'Захват не выполнен. Проверьте права dumpcap/Npcap.'

    def capture_state(self, identifier):
        with self.lock:
            job = self.jobs.get(identifier)
            if not job:
                raise ValueError('Захват не найден')
            return {k:v for k,v in job.items() if k!='process'}
