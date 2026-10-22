#!/usr/bin/env python3
"""LinkRadar. Local-only, dependency-free network stability recorder."""
import argparse
import csv
from contextlib import contextmanager
import io
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import shutil
import dns_resolver
import socket
import sqlite3
import statistics
import subprocess
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import error_log
import interface_probes
import service_paths
from route_engine import trace_cycle
from power_guard import PowerGuard
from packet_tools import PacketTools

ROOT = Path(__file__).resolve().parent
VERSION = "2.10.10"
RETENTION_SECONDS = 12 * 3600
# A measurement thread that ends early is restarted by the session timer at most this often.
WATCHDOG_SECONDS = 5
WORKER_RESTARTS = 3


def host_value(value):
    value = str(value).strip()
    if len(value) > 253 or not value or any(c in value for c in "/\\:@?# \t\r\n"):
        # Colons are allowed only in a validated literal IPv6 address.
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            raise ValueError("Укажите IP или имя хоста без протокола, пути и порта")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        value = value.rstrip(".").encode("idna").decode("ascii")
        if not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", s)
                   for s in value.split(".")):
            raise ValueError("Некорректное имя хоста")
        return value


def number(data, key, default, low, high):
    try:
        value = float(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError("Некорректное число: " + key)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Недопустимое значение: " + key)
    return value


def session_deadline(config,started):
    return math.inf if config.get('continuous') else started+config['duration_minutes']*60

def validate_config(data):
    targets = data.get("targets", [])
    if not isinstance(targets, list) or not 1 <= len(targets) <= 12:
        raise ValueError("Добавьте от 1 до 12 целей")
    result = {
        "name": str(data.get("name", "Проверка площадки")).strip()[:120] or "Проверка площадки",
        "profile": str(data.get("profile", "broadcast"))[:40],
        "interval": number(data, "interval", 1, 0.5, 30),
        "timeout": number(data, "timeout", 1, 0.2, 5),
        "duration_minutes": number(data, "duration_minutes", 15, 1, 1440),
        "continuous": bool(data.get('continuous',False)),
        "loss_limit": number(data, "loss_limit", 0.5, 0, 100),
        "latency_limit": number(data, "latency_limit", 100, 1, 10000),
        "jitter_limit": number(data, "jitter_limit", 20, 0, 10000),
        "burst_limit": int(number(data, "burst_limit", 2, 0, 1000)),
        "route_interval": number(data, "route_interval", 2.5, 0.5, 60),
        "max_hops": int(number(data, "max_hops", 20, 1, 40)),
        "interface": interface_probes.validate_interface(data.get("interface")),
        "targets": [],
    }
    if result["timeout"] > result["interval"]:
        raise ValueError("Таймаут должен быть не больше интервала измерений")
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("Некорректная цель")
        mode = target.get("mode", "icmp")
        if mode not in ("icmp", "tcp", "route"):
            raise ValueError("Поддерживаются ICMP, TCP и ICMP-маршрут")
        family = target.get("family", "ipv4")
        if family not in ("auto", "ipv4", "ipv6"):
            raise ValueError("Выберите IPv4, IPv6 или авто")
        address = str(target.get("address", "")).strip()
        if address:
            address = str(ipaddress.ip_address(address))
            if family != "auto" and (":" in address) != (family == "ipv6"):
                raise ValueError("Фиксированный IP не соответствует выбранному семейству")
        result["targets"].append({
            "id": uuid.uuid4().hex[:12],
            "host": host_value(target.get("host", "")),
            "label": str(target.get("label", "")).strip()[:80] or "Цель",
            "mode": mode,
            "family": family,
            "address": address,
            "port": int(number(target, "port", 443, 1, 65535)),
            "required": bool(target.get("required", True)),
        })
    if not any(t["required"] for t in result["targets"]):
        raise ValueError("Отметьте хотя бы одну обязательную цель")
    return result


def resolve(host, mode, family=None):
    family = family or ("ipv4" if mode in ("icmp", "route") else "auto")
    af = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6, "auto": socket.AF_UNSPEC}[family]
    results = dns_resolver.getaddrinfo(host, None, af, socket.SOCK_STREAM)
    endpoint=results[0][4]
    return endpoint[0]+('%'+str(endpoint[3]) if len(endpoint)==4 and endpoint[3] else '')


def ping_command(ip, timeout, system=None):
    system = system or platform.system()
    v6 = ipaddress.ip_address(ip).version == 6
    executable = shutil.which("ping") or ("/sbin/ping" if system == "Darwin" else "ping")
    if system == "Windows":
        return [executable, "-6" if v6 else "-4", "-n", "1", "-w", str(int(timeout * 1000)), ip]
    if system == "Darwin":
        if v6:
            return [shutil.which("ping6") or "/sbin/ping6", "-n", "-c", "1", ip]
        return [executable, "-n", "-c", "1", "-W", str(int(timeout * 1000)), ip]
    return [executable] + (["-6"] if v6 else []) + ["-n", "-c", "1", "-W", str(max(1, math.ceil(timeout))), ip]


def parse_ping(output, returncode):
    # Windows localized output: время=1мс; Unix: time=1.234 ms; <1ms is a bound.
    match = re.search(r"(?:time|время|temps|zeit|tiempo|tempo)\s*([=<])\s*([\d.,]+)\s*(?:ms|мс)",
                      output, re.I)
    if match:
        value = float(match.group(2).replace(",", "."))
        return {"status": "ok", "latency": value, "detail": "RTT < 1 мс" if match.group(1) == "<" else ""}
    lowered = output.lower()
    if any(s in lowered for s in ("not permitted", "permission denied", "general failure", "общий сбой",
                                  "requires elevation", "access is denied", "no route to host", "network is unreachable", "network is down")):
        return {"status": "error", "latency": None,
                "detail": "ОС запретила ICMP или вернула локальную ошибку. Проверьте права и сетевой стек."}
    if returncode == 0:
        # Never invent RTT from process execution time when a localized parser fails.
        return {"status": "error", "latency": None,
                "detail": "Ответ ping получен, но формат RTT не распознан"}
    return {"status": "timeout", "latency": None, "detail": "Нет ICMP-ответа / узел недоступен"}


def probe(target, timeout, ip=None, interface=None, stop=None):
    started = time.time()
    answer = {"status": "error", "latency": None, "detail": "", "ip": ip, "ts": started}
    try:
        ip = ip or target.get("address") or (interface_probes.resolve_bound(target["host"],target.get("family") or "ipv4",interface) if interface else resolve(target["host"], target["mode"], target.get("family")))
        answer["ip"] = ip
        if interface and target["mode"] != "tcp":
            binding=interface_probes.binding_for(interface,ip)
            reply=interface_probes.echo(ip,binding,timeout,stop=stop)
            answer.update(status="ok" if reply["kind"]=="echo" else "timeout" if reply["kind"]=="timeout" else "unreachable" if reply["kind"] in ("unreachable","ttl") else "error",latency=reply["latency"] if reply["kind"]=="echo" else None,detail=interface_probes.detail(binding,reply["kind"]))
        elif target["mode"] == "tcp":
            if interface:raise interface_probes.InterfaceUnavailable("Привязка TCP-проверки к адаптеру не поддерживается")
            tick = time.perf_counter()
            with socket.create_connection((ip, target["port"]), timeout=timeout):
                answer.update(status="ok", latency=round((time.perf_counter() - tick) * 1000, 3))
        else:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if platform.system() == "Windows" else {}
            process = subprocess.run(ping_command(ip, timeout), capture_output=True,
                                     timeout=timeout + 1.5, **kwargs)
            encoding = "oem" if platform.system() == "Windows" else "utf-8"
            output = (process.stdout + process.stderr).decode(encoding, errors="replace")
            answer.update(parse_ping(output, process.returncode))
    except interface_probes.InterfaceUnavailable as exc:
        answer["detail"] = str(exc)
    except socket.gaierror:
        answer["detail"] = "DNS: не удалось разрешить имя цели"
    except ConnectionRefusedError:
        answer.update(status="refused", detail="TCP-порт отклонил соединение")
    except (socket.timeout, TimeoutError, subprocess.TimeoutExpired):
        answer.update(status="timeout", detail="Истёк таймаут")
    except FileNotFoundError:
        answer["detail"] = "В системе не найден ping"
    except PermissionError:
        answer["detail"] = "ОС запретила сетевую проверку"
    except OSError:
        answer["detail"] = "Сетевая или локальная ошибка ОС"
    # Timestamp completion, rather than DNS/access preparation before the probe.
    answer["ts"]=time.time()
    return answer


from measurement_metrics import smooth_jitter, metrics, Jitter, cadence


def assessment(stats, config, elapsed):
    reasons = []
    if stats["attempted"] < 20 or elapsed < config["duration_minutes"] * 60:
        reasons.append("Период проверки ещё не завершён или меньше 20 измерений")
    expected = max(1, elapsed / config["interval"])
    coverage = min(100, stats["count"] / expected * 100)
    if coverage < 90:
        reasons.append("Недостаточное покрытие времени измерениями (< 90%)")
    if stats["max_gap"] > config["interval"] * 3 + 2:
        reasons.append("Были пропуски сбора данных: возможен сон или остановка компьютера")
    if stats["errors"]:
        reasons.append("Есть ошибки выполнения проверки")
    breaches = []
    for key, limit, description in (("loss", "loss_limit", "Доля неуспешных проверок"),
                                    ("p95", "latency_limit", "P95 задержки"),
                                    ("jitter", "jitter_limit", "Колебания задержки"),
                                    ("max_streak", "burst_limit", "Серия неуспешных проверок")):
        if stats[key] is not None and stats[key] > config[limit]:
            breaches.append(description + " выше порога")
    if breaches:
        return {"status": "risk", "reasons": breaches + reasons, "coverage": round(coverage, 1)}
    if reasons or not stats["ok"] or stats["jitter"] is None:
        return {"status": "pending", "reasons": reasons or ["Недостаточно успешных ответов"], "coverage": round(coverage, 1)}
    return {"status": "stable", "reasons": ["За проверенный период пороги не превышены"], "coverage": round(coverage, 1)}


class Accumulator:
    """Incremental session statistics. Keep RTTs only, never all sample dicts."""
    def __init__(self, interval=1):
        self.jitter_state=Jitter(interval);self.latest_jitter=None
        self.count = self.attempted = self.errors = self.failed = 0
        self.total = self.delta_sum = self.delta_count = 0
        self.streak = self.max_streak = self.max_gap = 0
        self.ordered = []
        self.last = None
        self.first_ts = None

    def add(self, sample, derived=False):
        self.latest_jitter,delta=(sample.get('jitter'),sample.get('delta')) if derived else self.jitter_state.add(sample)
        self.count += 1
        if self.first_ts is None:
            self.first_ts = sample["ts"]
        if self.last:
            self.max_gap = max(self.max_gap, sample["ts"] - self.last["ts"])
        if sample["status"] == "error":
            self.errors += 1
        else:
            self.attempted += 1
            if sample["status"] == "ok":
                self.total += sample["latency"]
                self.ordered.append(sample["latency"])
                if delta is not None:
                    self.delta_sum += delta
                    self.delta_count += 1
            else:
                self.failed += 1
        self.streak = self.streak + 1 if sample["status"] in ("timeout", "refused", "unreachable") else 0
        self.max_streak = max(self.max_streak, self.streak)
        self.last = sample

    def stats(self, started, ended):
        ordered=sorted(self.ordered)
        n = len(ordered)
        gap = max(self.max_gap, (self.first_ts - started) if self.first_ts is not None else 0,
                  ended - self.last["ts"] if self.last else ended - started)
        return {"count": self.count, "attempted": self.attempted, "ok": n, "failed": self.failed,
                "errors": self.errors, "loss": round(self.failed / self.attempted * 100, 3) if self.attempted else None,
                "avg": round(self.total / n, 2) if n else None,
                "min": round(ordered[0], 2) if n else None,
                "max": round(ordered[-1], 2) if n else None,
                "p95": round(ordered[math.ceil(n * .95) - 1], 2) if n else None,
                "jitter": self.latest_jitter,
                "mean_delta": round(self.delta_sum / self.delta_count, 2) if self.delta_count else None,
                "max_streak": self.max_streak, "max_gap": round(gap, 2)}


def parse_route(output):
    hops = []
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+(.+)", line)
        if not match:
            continue
        addresses = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", match.group(2))
        ip = next((a for a in addresses if valid_ipv4(a)), None)
        times = re.findall(r"<?\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)", match.group(2), re.I)
        hops.append({"hop": int(match.group(1)), "ip": ip,
                     "latency": float(times[0].replace(",", ".")) if times else None})
    return hops


def valid_ipv4(value):
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


class Store:
    def __init__(self, path, recover=True, retention_seconds=None, clock=time.time):
        self.event_cursor=0;self.path=Path(path);self.retention_seconds=retention_seconds;self.clock=clock;self.next_expiry=0
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started REAL, ended REAL, status TEXT, config TEXT);
            CREATE TABLE IF NOT EXISTS samples(session TEXT, target TEXT, ts REAL, status TEXT, latency REAL, ip TEXT, detail TEXT);
            CREATE INDEX IF NOT EXISTS sample_idx ON samples(session, target, ts);
            CREATE TABLE IF NOT EXISTS events(session TEXT, ts REAL, target TEXT, level TEXT, message TEXT);
            CREATE TABLE IF NOT EXISTS routes(session TEXT, target TEXT, ts REAL, state TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS hop_samples(session TEXT, target TEXT, ts REAL, cycle TEXT, hop INTEGER, node TEXT, ip TEXT, status TEXT, latency REAL, detail TEXT);
            CREATE INDEX IF NOT EXISTS hop_idx ON hop_samples(session,target,ts,hop);
            CREATE TABLE IF NOT EXISTS session_data(session TEXT, kind TEXT, key TEXT, ts REAL, data TEXT, PRIMARY KEY(session,kind,key));
            CREATE INDEX IF NOT EXISTS sample_time_idx ON samples(ts);
            CREATE INDEX IF NOT EXISTS hop_time_idx ON hop_samples(ts);
            CREATE INDEX IF NOT EXISTS hop_node_idx ON hop_samples(session,target,node,ts,hop,status,latency);
            CREATE INDEX IF NOT EXISTS route_time_idx ON routes(ts);
            CREATE INDEX IF NOT EXISTS route_target_idx ON routes(session,target,ts);
            CREATE INDEX IF NOT EXISTS event_time_idx ON events(ts);
            CREATE INDEX IF NOT EXISTS extra_time_idx ON session_data(ts);
            CREATE TABLE IF NOT EXISTS store_state(key TEXT PRIMARY KEY,value INTEGER);
        """)
        self._upgrade_hop_index()
        saved=self.db.execute("SELECT value FROM store_state WHERE key='event_cursor'").fetchone()
        if saved:self.event_cursor=int(saved[0])
        if retention_seconds:self.db.execute('PRAGMA secure_delete=ON')
        if recover:
            self.db.execute("UPDATE sessions SET status='interrupted', ended=COALESCE((SELECT MAX(ts) FROM samples WHERE session=sessions.id), started) WHERE status='running'")
            self.db.execute("DELETE FROM events")
        self.db.commit()
        self.prune()

    def _upgrade_hop_index(self):
        # Replace the old lookup index atomically. The covering suffix speeds
        # up wide-window aggregates without keeping a second copy of the index.
        columns=tuple(row[2] for row in self.db.execute('PRAGMA index_info(hop_node_idx)'))
        if columns==('session','target','node','ts','hop','status','latency'):return
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DROP INDEX hop_node_idx')
            self.db.execute('CREATE INDEX hop_node_idx ON hop_samples(session,target,node,ts,hop,status,latency)')
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _prune_locked(self):
        if not self.retention_seconds or self.clock()<=self.next_expiry:return
        cutoff=self.clock()-self.retention_seconds
        for table in ('samples','hop_samples','routes','events'):
            self.db.execute('DELETE FROM '+table+' WHERE ts<?',(cutoff,))
        self.db.execute("DELETE FROM session_data WHERE ts<? AND kind!='engine_state'",(cutoff,))
        self.db.execute("DELETE FROM sessions WHERE ended<? AND status!='running'",(cutoff,))
        self.db.execute("DELETE FROM session_data WHERE kind='engine_state' AND session NOT IN (SELECT id FROM sessions)")
        times=[self.db.execute('SELECT MIN(ts) FROM '+table).fetchone()[0] for table in ('samples','hop_samples','routes','events')]
        times.append(self.db.execute("SELECT MIN(ts) FROM session_data WHERE kind!='engine_state'").fetchone()[0])
        self.next_expiry=min((ts+self.retention_seconds for ts in times if ts is not None),default=math.inf)

    def prune(self,force=False):
        with self.lock:
            if force:self.next_expiry=0
            self._prune_locked();self.db.commit()

    def clear(self):
        with self.lock:
            self.db.execute('PRAGMA secure_delete=ON')
            for table in ('samples','hop_samples','routes','events','session_data','sessions'):
                self.db.execute('DELETE FROM '+table)
            self.db.commit();self.next_expiry=math.inf
            self.db.execute('VACUUM');self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        for name in ('service-out.log','service-error.log'):
            # Never truncate through a link, junction or extra hard link (audit item 3).
            path=self.path.parent/name
            if service_paths.plain_file(path):path.write_text('',encoding='utf-8')

    def execute(self, sql, args=()):
        with self.lock:
            cursor = self.db.execute(sql, args)
            if self.retention_seconds:
                match=re.match(r'^\s*INSERT(?: OR REPLACE)? INTO (samples|hop_samples|routes|events|session_data)\b',sql,re.I)
                if match and args:
                    index=3 if match[1].lower()=='session_data' else (2 if 'events(rowid' in sql.lower() else 1) if match[1].lower()=='events' else 2
                    if match[1].lower()!='session_data' or args[1]!='engine_state':self.next_expiry=min(self.next_expiry,float(args[index])+self.retention_seconds)
                self._prune_locked()
            self.db.commit()
            return cursor

    def rows(self, sql, args=()):
        with self.lock:
            if not self.db.in_transaction:self._prune_locked();self.db.commit()
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    @contextmanager
    def reader(self):
        """A consistent WAL read snapshot with no Python writer lock."""
        connection=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
        connection.row_factory=sqlite3.Row
        try:
            connection.execute('BEGIN')
            yield connection
        finally:
            try:connection.set_progress_handler(None,0);connection.rollback()
            finally:connection.close()

    def event(self, sid, target, level, message):
        with self.lock:
            # Rowids must not rewind after retention deletes the final event.
            self.event_cursor=max(self.event_cursor,self.db.execute('SELECT coalesce(max(rowid),0) FROM events').fetchone()[0])+1
            self.db.execute("INSERT OR REPLACE INTO store_state VALUES ('event_cursor',?)",(self.event_cursor,))
            self.execute("INSERT INTO events(rowid,session,ts,target,level,message) VALUES (?,?,?,?,?,?)", (self.event_cursor,sid,time.time(),target,level,message))


class ProbeStop:
    def __init__(self,stop,pause,changed=None):self.stop=stop;self.pause=pause;self.changed=changed
    def is_set(self):return self.stop.is_set() or self.pause.is_set() or bool(self.changed and self.changed())


class Monitor:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.lifecycle=threading.RLock()
        self.sid = None
        self.stop_event = threading.Event()
        self.workers = []
        self.timer_thread = None
        self.route_jobs = set()
        self.accumulators = {}
        self.latest_samples={};self.route_epoch={}
        self.history_stats = {}
        self.paused = False
        self.pause_at = None
        self.probe_cancel = threading.Event()
        self.config = None
        self.ptr_names = {}
        self.ptr_epoch=0
        self.ptr_slots = threading.BoundedSemaphore(4)
        self.power=PowerGuard()

    def hostname(self, ip):
        if not ip:
            return None
        with self.lock:
            if ip in self.ptr_names:
                return self.ptr_names[ip]
            if not self.ptr_slots.acquire(blocking=False):
                return ip
            if len(self.ptr_names) > 1000:
                self.ptr_names.clear()
            self.ptr_names[ip] = ip
            epoch=self.ptr_epoch
        def lookup():
            try:
                name = socket.gethostbyaddr(ip)[0][:253]
            except OSError:
                name = ip
            finally:
                self.ptr_slots.release()
            with self.lock:
                if self.ptr_epoch==epoch:self.ptr_names[ip] = name
        threading.Thread(target=lookup, daemon=True).start()
        return ip

    def pause(self, sid):
        with self.lock:
            if not self.sid or self.sid != sid:
                raise ValueError("Пауза доступна только для текущей сессии")
            self.paused = not self.paused
            self.pause_at=time.time() if self.paused else None
            if self.paused:self.probe_cancel.set();self.power.stop()
            else:
                self.probe_cancel=threading.Event()
                try:self.power.start()
                except OSError:self.store.event(sid,'','warning','Не удалось запретить автоматический сон')
            self.store.execute('INSERT OR REPLACE INTO session_data VALUES (?,?,?,?,?)',(sid,'engine_state','pause',time.time(),json.dumps({'paused':self.paused,'pause_at':self.pause_at})))
            self.store.event(sid, "", "info", "Сбор на паузе; история сохранена" if self.paused else "Сбор продолжен после паузы")
            return self.paused

    def set_interval(self,seconds):
        with self.lock:
            if not self.sid or self.stop_event.is_set():raise ValueError('Нет активной сессии')
            updated=validate_config(dict(self.config,interval=seconds,route_interval=seconds,timeout=min(1,float(seconds))))
            for key in ('interval','route_interval','timeout'):self.config[key]=updated[key]
            self.store.execute('UPDATE sessions SET config=? WHERE id=?',(json.dumps(self.config,ensure_ascii=False),self.sid))
            self.store.event(self.sid,'','info','Интервал опроса изменён: %s с; история сохранена'%seconds)

    def set_interface(self,identity):
        identity=interface_probes.validate_interface(identity)
        with self.lock:
            if not self.sid or self.stop_event.is_set():raise ValueError('Нет активной сессии')
            if self.config.get('interface')==identity:return
            self.probe_cancel.set()
            self.latest_samples.clear()
            self.config['interface']=identity
            self.config['interface_changed_at']=time.time()
            self.probe_cancel=threading.Event()
            if self.paused:self.probe_cancel.set()
            self.store.execute('UPDATE sessions SET config=? WHERE id=?',(json.dumps(self.config,ensure_ascii=False),self.sid))
            for target in self.config['targets']:
                data={'hops':[],'destination':None,'message':'Переключение адаптера: '+(identity or 'автоматически'),'interface':identity}
                self.store.execute('INSERT INTO routes VALUES (?,?,?,?,?)',(self.sid,target['id'],time.time(),'pending',json.dumps(data,ensure_ascii=False)))
            self.store.event(self.sid,'','info','Адаптер измерений изменён: '+(identity or 'автоматически')+'; история сохранена')

    def start(self, data):
        with self.lifecycle:return self._start(data)

    def _start(self, data):
        config = validate_config(data)
        with self.lock:
            if self.sid or any(worker.is_alive() for worker in self.workers):
                raise ValueError("Сначала остановите текущую проверку")
            sid = uuid.uuid4().hex
            started = time.time()
            self.store.execute("DELETE FROM events")
            self.store.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                               (sid, started, None, "running", json.dumps(config, ensure_ascii=False)))
            self.sid = sid
            self.paused = False
            self.pause_at=None;self.probe_cancel=threading.Event();self.config=config
            self.accumulators = {(sid, t["id"]): Accumulator(config["route_interval"] if t["mode"]=="route" else config["interval"]) for t in config["targets"]}
            self.latest_samples={};self.route_epoch={}
            self.stop_event = threading.Event()
            stop = self.stop_event
            self.workers = self.session_workers(sid, config, started, stop)
            for worker in self.workers:
                worker.start()
            self.timer_thread=threading.Thread(target=self.timer, args=(sid, config, started, stop, self.workers[:]), daemon=True);self.timer_thread.start()
            self.store.event(sid, "", "info", "Проверка запущена. Измерения выполняются с этого компьютера.")
            try:self.power.start()
            except OSError:self.store.event(sid,'','warning','Не удалось запретить автоматический сон; проверьте питание ОС')
            return sid

    def session_workers(self, sid, config, started, stop):
        workers = []
        for target in config["targets"]:
            workers.append(self.worker_thread(self.route_worker if target["mode"] == "route" else self.worker,
                                              (sid, target, config, started, stop)))
            if target["mode"] == "icmp":
                workers.append(self.worker_thread(self.background_route, (sid, target, config, started, stop)))
        return workers

    def worker_thread(self, function, args, restarts=0):
        def run():
            try:
                function(*args)
            except Exception as exc:
                error_log.error("Поток измерения %s цели %s остановлен" % (getattr(function, "__name__", "?"), args[1]["id"]), exc)
                self.safe_event(args[0], args[1]["id"], "error", "Измерение остановлено внутренней ошибкой (%s: %s)" % (type(exc).__name__, str(exc)[:200]))
        thread = threading.Thread(target=run, daemon=True)
        thread.spec = (function, args)
        thread.restarts = restarts
        return thread

    def safe_event(self, sid, target, level, message):
        """Event write that never raises: used while handling another error."""
        try:
            self.store.event(sid, target, level, message)
        except Exception:
            pass

    def internal_error(self, sid, target, exc, failing):
        """One event per failure streak; the worker waits for its next tick and retries."""
        if not failing:
            error_log.error("Внутренняя ошибка измерения цели %s" % target["id"], exc)
            self.safe_event(sid, target["id"], "error", "Внутренняя ошибка измерения (%s: %s); измерение продолжается" % (type(exc).__name__, str(exc)[:200]))
        return True

    def recovered(self, sid, target, failing):
        if failing:
            self.safe_event(sid, target["id"], "info", "Измерение восстановилось после внутренней ошибки")
        return False

    def revive(self, sid, workers, stop, deadline):
        """Restart a worker that ended before the deadline without a stop request."""
        for index, worker in enumerate(workers):
            if worker.is_alive() or stop.is_set() or time.time() >= deadline - 1 or worker.restarts >= WORKER_RESTARTS:
                continue
            function, args = worker.spec
            replacement = self.worker_thread(function, args, worker.restarts + 1)
            workers[index] = replacement
            with self.lock:
                if self.sid == sid and worker in self.workers:
                    self.workers[self.workers.index(worker)] = replacement
            error_log.logger.warning("Поток измерения цели %s перезапущен (%d из %d)", args[1]["id"], replacement.restarts, WORKER_RESTARTS)
            self.safe_event(sid, args[1]["id"], "warning", "Поток измерения перезапущен (%d из %d)" % (replacement.restarts, WORKER_RESTARTS))
            replacement.start()

    def background_route(self, sid, target, config, started, stop):
        key = (sid, target["id"])
        with self.lock:
            self.route_jobs.add(key)
        try:
            self.route_worker(sid, target, config, started, stop, record_target=False)
        finally:
            with self.lock:
                self.route_jobs.discard(key)

    def resume(self, sid, paused=False):
        with self.lifecycle,self.lock:return self._resume(sid,paused)

    def _resume(self, sid, paused=False):
        """Explicit update recovery, preserving the original session deadline and samples."""
        rows = self.store.rows("SELECT * FROM sessions WHERE id=?", (sid,))
        if not rows or self.sid:
            raise ValueError("Сессия для восстановления не найдена или уже идёт сбор")
        row = rows[0]
        config = json.loads(row["config"])
        self.config=config
        started = row["started"]
        if time.time() >= session_deadline(config,started):
            raise ValueError("Заданный период уже завершился; создайте новую проверку")
        self.accumulators = {}
        self.latest_samples={};self.route_epoch={}
        for target in config["targets"]:
            accumulator = Accumulator(config["route_interval"] if target["mode"]=="route" else config["interval"])
            if not self.store.retention_seconds:
                for sample in self.store.rows("SELECT ts,status,latency,ip,detail FROM samples WHERE session=? AND target=? ORDER BY ts", (sid, target["id"])):
                    accumulator.add(sample)
            self.accumulators[(sid, target["id"])] = accumulator
            latest=self.store.rows("SELECT ts,status,latency,ip,detail FROM samples WHERE session=? AND target=? AND status!='error' AND ip IS NOT NULL ORDER BY ts DESC LIMIT 1",(sid,target['id']))
            if latest and latest[0]['ts']>=config.get('interface_changed_at',0):self.latest_samples[(sid,target['id'])]=latest[0]
        self.store.execute("UPDATE sessions SET status='running',ended=NULL WHERE id=?", (sid,))
        self.sid = sid
        self.paused = paused
        saved=self.store.rows("SELECT data FROM session_data WHERE session=? AND kind='engine_state' AND key='pause'",(sid,))
        state=json.loads(saved[0]['data']) if saved else {}
        self.paused=bool(paused or state.get('paused'));self.pause_at=(state.get('pause_at') or time.time()) if self.paused else None
        self.probe_cancel=threading.Event()
        if self.paused:self.probe_cancel.set()
        self.stop_event = threading.Event()
        stop = self.stop_event
        self.workers = self.session_workers(sid, config, started, stop)
        self.store.event(sid, "", "info", "Сбор возобновлён после обновления приложения; исходные измерения и срок сохранены, разрыв сбора не заполнялся")
        for worker in self.workers:
            worker.start()
        self.timer_thread=threading.Thread(target=self.timer, args=(sid, config, started, stop, self.workers[:]), daemon=True);self.timer_thread.start()
        try:
            if not self.paused:self.power.start()
        except OSError:self.store.event(sid,'','warning','Не удалось запретить автоматический сон')

    def timer(self, sid, config, started, stop, workers):
        deadline = session_deadline(config, started)
        while True:
            if stop.wait(min(WATCHDOG_SECONDS, max(0, deadline - time.time()))):
                completed = False
                break
            if time.time() >= deadline:
                completed = True
                break
            self.revive(sid, workers, stop, deadline)
        stop.set()
        for worker in workers:
            worker.join(timeout=config["timeout"] + 2)
        # Keep the session in stopping state until every original worker exits.
        # Otherwise a new start can overwrite self.workers while an old one is
        # still running, making later cleanup unable to see it.
        while any(worker.is_alive() for worker in workers):
            for worker in workers:worker.join(timeout=.1)
        # Finished worker samples must never expand the planned analysis window.
        ended = min(time.time(),session_deadline(config,started)) if completed else time.time()
        with self.lock:
            self.store.execute("UPDATE sessions SET ended=?,status=? WHERE id=?",
                               (ended, "completed" if completed else "stopped", sid))
            self.store.event(sid, "", "info", "Период завершён" if completed else "Проверка остановлена вручную")
            if self.sid == sid:
                self.sid = None
                self.power.stop()

    def stop(self):
        with self.lock:
            self.stop_event.set()

    def clear(self):
        with self.lifecycle:return self._clear()

    def _clear(self):
        self.stop()
        if self.timer_thread and self.timer_thread.is_alive():self.timer_thread.join(12)
        if (self.timer_thread and self.timer_thread.is_alive()) or any(worker.is_alive() for worker in self.workers):
            raise RuntimeError('Мониторинг ещё завершается. Повторите закрытие через несколько секунд.')
        with self.lock:
            self.store.clear();self.sid=None;self.paused=False;self.pause_at=None;self.config=None
            self.accumulators.clear();self.history_stats.clear();self.ptr_names.clear()
            self.ptr_epoch+=1
            self.latest_samples.clear();self.route_epoch.clear()

    def route_worker(self, sid, target, config, started, stop, record_target=True):
        known = {}
        failing = False
        last_signature = None
        previous_ip = None
        previous_interface = None
        while not stop.is_set() and time.time() < session_deadline(config,started):
            if self.paused:
                stop.wait(.1)
                continue
            tick = time.monotonic()
            with self.lock:
                cancel=ProbeStop(stop,self.probe_cancel);identity=config.get("interface")
            if cancel.is_set():continue
            try:
                if identity!=previous_interface:known.clear();last_signature=None;previous_ip=None;previous_interface=identity
                cycle_id = uuid.uuid4().hex[:12]
                try:
                    with self.lock:
                        latest=self.latest_samples.get((sid,target['id']))
                        ping_ip=latest.get('ip') if not record_target and latest else None
                    if not record_target and not (target.get('address') or ping_ip):
                        try:
                            literal=ipaddress.ip_address(target['host'])
                            if target.get('family') not in ('auto',None) and (literal.version==6)!=(target['family']=='ipv6'):
                                raise interface_probes.InterfaceUnavailable('Семейство IP цели не совпадает с настройкой проверки')
                            ping_ip=str(literal)
                        except ValueError:
                            # A hostname must follow the IP actually used by ping.
                            # Independent DNS here can trace a different load-balanced host.
                            stop.wait(.05);continue
                    ip = target.get("address") or ping_ip or (interface_probes.resolve_bound(target["host"],target.get("family") or "ipv4",identity) if identity else resolve(target["host"], "route", target.get("family")))
                    if not record_target:
                        def changed():
                            current=self.latest_samples.get((sid,target['id']))
                            return bool(current and not interface_probes.same_ip(current.get('ip'),ip))
                        cancel=ProbeStop(cancel.stop,cancel.pause,changed)
                    if previous_ip and ip != previous_ip:
                        known.clear()
                        self.store.event(sid, target["id"], "info", "Изменился IP цели маршрута; история узлов разделена")
                    previous_ip = ip
                    def save_hop(hop):
                        with self.lock:
                            if cancel.is_set() or hop["ts"] > session_deadline(config,started):return
                            if hop["ip"]:known[hop["hop"]] = hop["ip"]
                            node = "%d:%s:%s" % (hop["hop"], known.get(hop["hop"], "?"), ip)
                            if identity:node += ":"+interface_probes.path_key(hop)
                            self.store.execute("INSERT INTO hop_samples VALUES (?,?,?,?,?,?,?,?,?,?)",
                                               (sid, target["id"], hop["ts"], cycle_id, hop["hop"], node,
                                                hop["ip"], hop["status"], hop["latency"], hop["detail"]))
                    result = trace_cycle(ip, config["timeout"], config["max_hops"], cancel, save_hop,interface=identity) if identity else trace_cycle(ip, config["timeout"], config["max_hops"], cancel, save_hop)
                    if stop.is_set():
                        break
                    if cancel.is_set():continue
                    with self.lock:
                        if cancel.is_set():continue
                        self.store.execute("INSERT INTO routes VALUES (?,?,?,?,?)",
                                           (sid, target["id"], time.time(), result["state"], json.dumps(result, ensure_ascii=False)))
                    final = next((h for h in result["hops"] if interface_probes.same_ip(h["ip"],ip) and h["status"] == "ok"), None)
                    if final:
                        known = {hop: address for hop, address in known.items() if hop <= final["hop"]}
                        sample = dict(final, detail=final.get('detail','')+" · Конечная цель в ICMP-маршруте")
                    else:
                        # A route cut off before the target isn't proof of destination packet loss.
                        sample = {"ts": time.time(), "ip": ip, "latency": None, "status": "error",
                                  "detail": "В цикле маршрута нет подтверждённого измерения конечной цели; оцените отдельным ICMP/TCP"}
                    signature = tuple(sorted(known.items()))
                    if last_signature and signature != last_signature:
                        self.store.event(sid, target["id"], "info", "Изменились отвечающие адреса в маршруте")
                    last_signature = signature
                except (OSError, subprocess.SubprocessError) as exc:
                    sample = {"ts": time.time(), "ip": None, "latency": None, "status": "error",
                              "detail": str(exc) if isinstance(exc,interface_probes.InterfaceUnavailable) else "ICMP-маршрут недоступен: утилита, DNS или разрешения ОС"}
                    if not record_target and not cancel.is_set():
                        data = {"hops": [], "destination": previous_ip, "message": sample["detail"]}
                        with self.lock:
                            if not cancel.is_set():self.store.execute("INSERT INTO routes VALUES (?,?,?,?,?)",
                                           (sid, target["id"], time.time(), "error", json.dumps(data, ensure_ascii=False)))
                if stop.is_set() or sample["ts"] > session_deadline(config,started):
                    break
                if cancel.is_set():continue
                if record_target:
                    with self.lock:
                        if cancel.is_set():continue
                        self.store.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?)",
                                           (sid, target["id"], sample["ts"], sample["status"], sample["latency"], sample["ip"], sample["detail"]))
                        if not self.store.retention_seconds:self.accumulators[(sid, target["id"])].add(sample)
                failing = self.recovered(sid, target, failing)
            except Exception as exc:
                # An unexpected error (e.g. SQLite I/O) must not end the measurement silently.
                failing = self.internal_error(sid, target, exc, failing)
            stop.wait(max(0, config["route_interval"] - (time.monotonic() - tick)))

    def worker(self, sid, target, config, started, stop):
        next_tick = time.monotonic()
        last_state = None
        outage_start = None
        ip = None
        resolve_at = 0
        recent = []
        failing = False
        end = session_deadline(config,started)
        while not stop.is_set() and time.time() < end:
            if self.paused:
                stop.wait(.1)
                next_tick = time.monotonic()
                continue
            try:
                if time.time() >= resolve_at:
                    ip = None
                    resolve_at = time.time() + 60
                with self.lock:
                    cancel=ProbeStop(stop,self.probe_cancel);identity=config.get("interface")
                if cancel.is_set():continue
                sample = probe(target, config["timeout"], ip,interface=identity,stop=cancel) if identity else probe(target, config["timeout"], ip)
                if stop.is_set() or time.time() > end:
                    break
                if cancel.is_set():continue
                if sample["status"] != "error":
                    if ip and ip != sample["ip"]:
                        self.store.event(sid, target["id"], "info", "Изменился IP цели")
                    ip = sample["ip"]
                else:
                    resolve_at = 0
                with self.lock:
                    if cancel.is_set():continue
                    self.store.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?)",
                                       (sid, target["id"], sample["ts"], sample["status"], sample["latency"], sample["ip"], sample["detail"]))
                    if not self.store.retention_seconds:self.accumulators[(sid, target["id"])].add(sample)
                    if sample['status']!='error' and sample.get('ip'):
                        key=(sid,target['id']);previous=self.latest_samples.get(key)
                        if previous and not interface_probes.same_ip(previous.get('ip'),sample['ip']):self.route_epoch[key]=sample['ts']
                        self.latest_samples[key]=dict(sample)
                recent.append(sample)
                recent = [s for s in recent if s["ts"] >= sample["ts"] - 60]
                state = sample["status"]
                if state != "ok":
                    if state != last_state:
                        self.store.event(sid, target["id"], "error" if state == "error" else "warning", sample["detail"])
                    if outage_start is None:
                        outage_start = sample["ts"]
                elif last_state in ("timeout", "refused", "unreachable", "error"):
                    self.store.event(sid, target["id"], "info", "Ответы восстановились; период без успешных ответов ≈ %.1f с" % (sample["ts"] - outage_start))
                    outage_start = None
                elif sample["latency"] > config["latency_limit"] and last_state != "latency":
                    self.store.event(sid, target["id"], "warning", "Задержка выше порога: %.1f мс" % sample["latency"])
                    state = "latency"
                elif last_state == "latency" and sample["latency"] <= config["latency_limit"]:
                    self.store.event(sid, target["id"], "info", "Задержка вернулась ниже порога")
                # Keep the latency state across successive high samples; avoid notification storms.
                if state == "ok" and sample["latency"] > config["latency_limit"]:
                    state = "latency"
                last_state = state
                failing = self.recovered(sid, target, failing)
            except Exception as exc:
                # An unexpected error (e.g. SQLite I/O) must not end the measurement silently.
                failing = self.internal_error(sid, target, exc, failing)
            next_tick += config["interval"]
            if next_tick < time.monotonic():
                next_tick = time.monotonic()
            stop.wait(max(0, next_tick - time.monotonic()))

    def history(self):
        rows = self.store.rows("SELECT * FROM sessions ORDER BY started DESC LIMIT 100")
        for row in rows:
            config = json.loads(row.pop("config"))
            row.update(name=config["name"], profile=config["profile"], targets=len(config["targets"]))
        return rows

    def snapshot(self, sid=None, window=300, focus_end=None, event_limit=200, compact=False, selected_node=None, event_after=0, cancel=None):
        if compact and sid:
            import compact_snapshot
            return compact_snapshot.snapshot(self,sid,window,focus_end,event_after,selected_node,cancel)
        if not sid:
            history = self.history()
            sid = self.sid or (history[0]["id"] if history else None)
        rows = self.store.rows("SELECT * FROM sessions WHERE id=?", (sid,))
        if not rows:
            return {"session": None, "targets": [], "events": [], "active_id": self.sid}
        session = rows[0]
        config = json.loads(session.pop("config"))
        session["config"] = config
        end = session["ended"] or (self.pause_at if self.sid==sid and self.paused and self.pause_at else time.time())
        elapsed = max(0, end - session["started"])
        session["elapsed"] = elapsed
        session["remaining"] = None if config.get('continuous') else max(0,config['duration_minutes']*60-elapsed)
        analysis_end = min(end, max(session["started"], focus_end)) if focus_end is not None else end
        analysis_start = max(session["started"], analysis_end - window)
        session.update(analysis_start=analysis_start, analysis_end=analysis_end)
        targets = []
        for target in config["targets"]:
            key = (sid, target["id"])
            target_interval=config['interval']
            window_interval=target_interval
            if target['mode']=='route':
                recent=self.store.rows('SELECT ts FROM samples WHERE session=? AND target=? AND ts<=? ORDER BY ts DESC LIMIT 257',(sid,target['id'],end))
                target_interval=cadence([p['ts'] for p in reversed(recent)],config['route_interval'])
                recent=self.store.rows('SELECT ts FROM samples WHERE session=? AND target=? AND ts<=? ORDER BY ts DESC LIMIT 257',(sid,target['id'],analysis_end)) if analysis_end!=end else recent
                window_interval=cadence([p['ts'] for p in reversed(recent)],config['route_interval'])
            with self.lock:
                accumulator = self.accumulators.get(key) if not self.store.retention_seconds and target["mode"]!="route" else None
                cached = self.history_stats.get(key) if not self.store.retention_seconds else None
                stats = accumulator.stats(session["started"], end) if accumulator else cached
            if stats is None:
                history_samples = self.store.rows("SELECT ts,status,latency,ip,detail FROM samples WHERE session=? AND target=? AND ts<=? ORDER BY ts",
                                                 (sid, target["id"], end))
                historical = Accumulator(target_interval)
                for sample in history_samples:
                    historical.add(sample)
                stats = historical.stats(max(session['started'],self.store.clock()-self.store.retention_seconds) if self.store.retention_seconds else session['started'], end)
                with self.lock:
                    # Cache only finalized sessions; memory remains bounded across history browsing.
                    if len(self.history_stats) > 120:
                        self.history_stats.clear()
                    if session["status"] != "running" and not self.store.retention_seconds:
                        self.history_stats[key] = stats
            target_config = dict(config, interval=target_interval) if target["mode"] == "route" else config
            verdict = assessment(stats, target_config, elapsed)
            # Interrupted sessions cannot become green even if the last sample was near the deadline.
            if session["status"] == "interrupted" and verdict["status"] == "stable":
                verdict.update(status="pending", reasons=["Сбор прервался при завершении приложения"])
            tail = self.store.rows("SELECT ts,status,latency,ip,detail FROM samples WHERE session=? AND target=? AND ts>=? AND ts<=? ORDER BY ts",
                                   (sid, target["id"], analysis_start, analysis_end))
            last_rows = self.store.rows("SELECT ts,status,latency,ip,detail FROM samples WHERE session=? AND target=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                                        (sid, target["id"], end))
            # 256 preceding probes warm up EWMA independently of viewport focus.
            prefix = self.store.rows("SELECT ts,status,latency,ip,detail FROM samples WHERE session=? AND target=? AND ts<? ORDER BY ts DESC LIMIT 256",
                                     (sid, target['id'], analysis_start))
            tail = smooth_jitter(list(reversed(prefix)) + tail, window_interval)[len(prefix):]
            # Rendering preserves both metric extrema and true source boundaries.
            from plot_sampling import probe_samples
            graph,target_graph_step=probe_samples(tail,window_interval)
            routes = self.store.rows("SELECT * FROM routes WHERE session=? AND target=? AND ts<=? ORDER BY ts DESC LIMIT 1", (sid, target["id"], analysis_end))
            route = routes[0] if routes else None
            if route:
                route["data"] = json.loads(route["data"])
            if focus_end is None and target['mode']=='icmp' and route and last_rows and last_rows[0]['ip'] and route['data'].get('destination') and not interface_probes.same_ip(last_rows[0]['ip'],route['data']['destination']):route=None
            current_route = None
            if focus_end is None and session["ended"]:
                newer = self.store.rows("SELECT * FROM routes WHERE session=? AND target=? AND ts>? ORDER BY ts DESC LIMIT 1",
                                        (sid, target["id"], analysis_end))
                if newer:
                    current_route = newer[0]
                    current_route["data"] = json.loads(current_route["data"])
            hop_rows = self.store.rows("SELECT ts,hop,node,ip,status,latency,detail,cycle FROM hop_samples WHERE session=? AND target=? AND ts>=? AND ts<=? ORDER BY ts",
                                       (sid, target["id"], analysis_start, analysis_end))
            grouped = {}
            latest_nodes = {}
            for row in hop_rows:
                grouped.setdefault(row["node"], []).append(row)
                if row["ts"]>=max(config.get("interface_changed_at",0),self.route_epoch.get((sid,target['id']),0) if focus_end is None else 0):latest_nodes[row["hop"]] = row["node"]
            hops = []
            for hop, node in sorted(latest_nodes.items()):
                if route and route["state"] == "complete" and hop > max((h["hop"] for h in route["data"]["hops"]), default=0):
                    continue
                points = grouped[node]
                last = points[-1]
                answered_ip = next((p["ip"] for p in reversed(points) if p["ip"]), None)
                recent=self.store.rows('SELECT ts FROM hop_samples WHERE session=? AND target=? AND node=? AND ts<=? ORDER BY ts DESC LIMIT 257',(sid,target['id'],node,analysis_end))
                measured_interval=cadence([p['ts'] for p in reversed(recent)],config.get('route_interval',2.5))
                prefix = self.store.rows("SELECT ts,status,latency,ip,detail FROM hop_samples WHERE session=? AND target=? AND node=? AND ts<? ORDER BY ts DESC LIMIT 256",
                                         (sid, target['id'], node, analysis_start))
                points = smooth_jitter(list(reversed(prefix)) + points, measured_interval)[len(prefix):]
                last = points[-1]
                hop_graph,step=probe_samples(points,measured_interval)
                hops.append({"hop": hop, "node": node, "ip": answered_ip, "last": last,
                             "name": self.hostname(answered_ip),
                             "stats": metrics(points,measured_interval), "graph": hop_graph, "graph_step": step,
                             "interval": round(measured_interval, 3), "destination": bool(route and interface_probes.same_ip(answered_ip,route["data"].get("destination")))})
            for hop in hops:hop['source']='route'
            targets.append(dict(target, stats=stats, window_stats=metrics(tail,window_interval), assessment=verdict,
                                window_last=tail[-1] if tail else None,
                                last=last_rows[0] if last_rows else None, graph=graph, graph_step=target_graph_step, route=route,
                                hops=hops, interval=window_interval,
                                current_route=current_route,
                                tracing=(sid, target["id"]) in self.route_jobs))
        required = [t for t in targets if t["required"]]
        status = "risk" if any(t["assessment"]["status"] == "risk" for t in required) else (
            "stable" if required and all(t["assessment"]["status"] == "stable" for t in required) else "pending")
        events = self.store.rows("SELECT * FROM events WHERE session=? ORDER BY ts DESC"+(' LIMIT ?' if event_limit is not None else ''), (sid,int(event_limit)) if event_limit is not None else (sid,))
        return {"session": session, "targets": targets, "events": events, "verdict": status,
                "active_id": self.sid, "paused": self.paused if self.sid == sid else False, "version": VERSION}

    def trace(self, sid, tid):
        snap = self.snapshot(sid)
        target = next((t for t in snap["targets"] if t["id"] == tid), None)
        if not target:
            raise ValueError("Цель не найдена")
        if target["mode"] in ("icmp", "route") and snap["session"]["status"] == "running":
            raise ValueError("Маршрут уже измеряется непрерывно")
        key = (sid, tid)
        with self.lock:
            if key in self.route_jobs:
                raise ValueError("Трассировка уже выполняется")
            self.route_jobs.add(key)
        threading.Thread(target=self.trace_worker, args=(sid, target, key), daemon=True).start()

    def trace_worker(self, sid, target, key):
        state = "error"
        data = {"hops": [], "message": ""}
        try:
            ip = target.get("address") or resolve(target["host"], "icmp", target.get("family"))
            data = trace_cycle(ip, 1, 20)
            state = data["state"]
        except (OSError, subprocess.SubprocessError):
            data["message"] = "ICMP-трассировка недоступна: DNS, утилита или права ОС"
        finally:
            self.store.execute("INSERT INTO routes VALUES (?,?,?,?,?)", (sid, target["id"], time.time(), state, json.dumps(data, ensure_ascii=False)))
            with self.lock:
                self.route_jobs.discard(key)


def csv_value(value):
    """Keep spreadsheet exports literal, including leading control characters."""
    if isinstance(value,str) and (value.startswith(('\t','\r','\n')) or value.lstrip().startswith(('=','+','-','@'))):return "'"+value
    return value


def csv_export(store, sid):
    sessions = store.rows("SELECT config FROM sessions WHERE id=?", (sid,))
    if not sessions:
        raise ValueError("Сессия не найдена")
    targets = {t["id"]: t for t in json.loads(sessions[0]["config"])["targets"]}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["utc", "unix_timestamp", "target", "host", "mode", "port", "resolved_ip", "status", "latency_ms", "detail"])
    for s in store.rows("SELECT * FROM samples WHERE session=? ORDER BY ts", (sid,)):
        t = targets[s["target"]]
        row = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s["ts"])), s["ts"], t["label"], t["host"],
               t["mode"], t["port"] if t["mode"] == "tcp" else "", s["ip"], s["status"], s["latency"], s["detail"]]
        # Spreadsheet formula injection protection for user supplied labels.
        writer.writerow([csv_value(v) for v in row])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def hop_csv_export(store, sid):
    if not store.rows("SELECT id FROM sessions WHERE id=?", (sid,)):
        raise ValueError("Сессия не найдена")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["utc","target_id","hop","node","observed_ip","status","rtt_ms","cycle","detail"])
    for r in store.rows("SELECT * FROM hop_samples WHERE session=? ORDER BY ts,hop", (sid,)):
        writer.writerow([csv_value(v) for v in [time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(r["ts"])), r["target"],r["hop"],r["node"],r["ip"],r["status"],r["latency"],r["cycle"],r["detail"]]])
    return ("\ufeff"+out.getvalue()).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)
    def log_message(self, *_):
        pass

    def send(self, body, mime="application/json; charset=utf-8", status=200, filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        hosts = {"127.0.0.1:%d" % self.server.server_port, "localhost:%d" % self.server.server_port}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in hosts}:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return self.send({"error": "Разрешён только локальный интерфейс"}, status=403)
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/":
                body = (ROOT / "static/index.html").read_text("utf-8").replace("__TOKEN__", self.server.token).encode("utf-8")
                return self.send(body, "text/html; charset=utf-8")
            if url.path in ("/app.js", "/route-graph.js", "/style.css"):
                mime = "text/javascript" if url.path.endswith(".js") else "text/css"
                return self.send((ROOT / "static" / url.path[1:]).read_bytes(), mime + "; charset=utf-8")
            if url.path.startswith("/api/") and self.headers.get("X-Local-Token") != self.server.token:
                return self.send({"error": "Обновите страницу приложения"}, status=403)
            sid = query.get("session", [None])[0]
            if url.path == "/api/state":
                window = float(query.get("window", [300])[0])
                if not math.isfinite(window) or not 60 <= window <= 86400:
                    raise ValueError("Недопустимый период")
                focus = query.get("end", [None])[0]
                focus = float(focus) if focus is not None else None
                if focus is not None and not math.isfinite(focus):
                    raise ValueError("Некорректное время фокуса")
                return self.send(self.server.monitor.snapshot(sid, window, focus))
            if url.path == "/api/history":
                return self.send(self.server.monitor.history())
            if url.path == "/api/export.csv":
                return self.send(csv_export(self.server.monitor.store, sid), "text/csv; charset=utf-8", filename="network-samples.csv")
            if url.path == "/api/hops.csv":
                return self.send(hop_csv_export(self.server.monitor.store, sid), "text/csv; charset=utf-8", filename="route-hops.csv")
            if url.path == "/api/packets/status":
                return self.send(self.server.packets.status())
            if url.path == "/api/packets/interfaces":
                return self.send(self.server.packets.interfaces())
            if url.path == "/api/packets/capture":
                return self.send(self.server.packets.capture_state(query.get("id", [None])[0]))
            if url.path == "/api/packets/file":
                return self.send(self.server.packets.path(query.get("id",[None])[0]).read_bytes(), "application/octet-stream",filename="network-capture.pcapng")
            if url.path == "/api/export.json":
                snapshot = self.server.monitor.snapshot(sid, 86400)
                if not snapshot["session"]:
                    raise ValueError("Сессия не найдена")
                snapshot["raw_samples"] = self.server.monitor.store.rows("SELECT * FROM samples WHERE session=? ORDER BY ts", (sid,))
                snapshot["all_events"] = self.server.monitor.store.rows("SELECT * FROM events WHERE session=? ORDER BY ts", (sid,))
                snapshot["all_routes"] = self.server.monitor.store.rows("SELECT * FROM routes WHERE session=? ORDER BY ts", (sid,))
                snapshot["hop_samples"] = self.server.monitor.store.rows("SELECT * FROM hop_samples WHERE session=? ORDER BY ts", (sid,))
                return self.send(snapshot, filename="network-session.json")
            if url.path == "/api/system":
                return self.send({"os": platform.system(), "hostname": socket.gethostname(), "version": VERSION})
            if url.path == "/api/resolve":
                host = host_value(query.get("host", [""])[0])
                try:
                    addresses = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)))
                except OSError:
                    raise ValueError("Не удалось разрешить имя хоста")
                return self.send({"addresses": addresses})
            return self.send({"error": "Не найдено"}, status=404)
        except (ValueError, TypeError) as exc:
            return self.send({"error": str(exc)}, status=400)
        except Exception as exc:
            return self.failed(exc)

    def failed(self, exc):
        error_log.error("Ошибка обработки запроса " + self.command + " " + urlsplit(self.path).path, exc)
        try:
            return self.send({"error": "Внутренняя ошибка приложения; подробности в журнале ошибок"}, status=500)
        except OSError:
            return None

    def do_POST(self):
        if not self.trusted() or self.headers.get("X-Local-Token") != self.server.token:
            return self.send({"error": "Запрос запрещён"}, status=403)
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:
                raise ValueError('Ожидается один Content-Length без Transfer-Encoding')
            length = int(self.headers.get("Content-Length", 0))
            if self.path == "/api/packets/upload":
                if not 24 <= length <= 50 * 1024 * 1024:
                    raise ValueError("Выберите PCAP/PCAPNG до 50 МБ")
                identifier = self.server.packets.upload(self.rfile.read(length))
                return self.send({"id":identifier})
            if not 0 < length <= 16384:
                raise ValueError("Недопустимый размер запроса")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Ожидается объект JSON")
            if self.path == "/api/start":
                return self.send({"id": self.server.monitor.start(body)})
            if self.path == "/api/stop":
                self.server.monitor.stop()
                return self.send({"ok": True})
            if self.path == "/api/pause":
                return self.send({"paused": self.server.monitor.pause(body.get("session"))})
            if self.path == "/api/trace":
                self.server.monitor.trace(body.get("session"), body.get("target"))
                return self.send({"ok": True})
            if self.path == "/api/packets/analyze":
                return self.send(self.server.packets.analyze(body.get("id"),body.get("filter","")))
            if self.path == "/api/packets/capture":
                identifier = self.server.packets.capture(body.get("interface"),body.get("ip"),body.get("seconds",30))
                return self.send({"id":identifier})
            if self.path == "/api/comment":
                sid = body.get("session")
                if not self.server.monitor.store.rows("SELECT id FROM sessions WHERE id=?", (sid,)):
                    raise ValueError("Сессия не найдена")
                message = str(body.get("message", "")).strip()[:500]
                if not message:
                    raise ValueError("Введите комментарий")
                self.server.monitor.store.event(sid, "", "note", message)
                return self.send({"ok": True})
            return self.send({"error": "Не найдено"}, status=404)
        except (ValueError, TypeError) as exc:
            return self.send({"error": str(exc)}, status=400)
        except Exception as exc:
            return self.failed(exc)


def main():
    parser = argparse.ArgumentParser(description="LinkRadar — локальная диагностика стабильности сети")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--resume-session", help="Явно восстановить указанную сессию после обновления, сохранив срок и историю")
    parser.add_argument("--resume-paused", action="store_true", help="Сохранить паузу восстанавливаемой сессии")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    error_log.setup(data, "web")
    # Prevent two engines from writing sessions into the same database.
    lock_file = (data / "engine.lock").open("a+b")
    try:
        if platform.system() == "Windows":
            import msvcrt
            lock_file.seek(0)
            if lock_file.read(1) == b"":
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        parser.exit(1, "Приложение уже использует эту папку данных. Откройте существующее окно.\n")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        parser.exit(1, "Порт занят. Откройте уже запущенное приложение или задайте --port.\n")
    server.daemon_threads = True
    server.token = secrets.token_hex(24)
    server.monitor = Monitor(Store(data / "network.sqlite3"))
    if args.resume_session:
        try:
            server.monitor.resume(args.resume_session, args.resume_paused)
        except ValueError as exc:
            parser.exit(1, str(exc) + "\n")
    server.packets = PacketTools(data)
    url = "http://127.0.0.1:%d" % args.port
    print("LinkRadar " + VERSION + " — " + url, flush=True)
    print("Данные: " + str(data.resolve()) + ". Для выхода: Ctrl+C.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.monitor.stop()
        for worker in server.monitor.workers:
            worker.join(timeout=2)
    finally:
        server.server_close()
        lock_file.close()


if __name__ == "__main__":
    main()
