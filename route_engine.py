"""ICMP route cycles via OS tools, with observed timestamps and cancellable processes."""
import interface_probes
import ipaddress
import math
import platform
import re
import shutil
import subprocess
import threading
import time


def parse_hop(line):
    match = re.match(r"\s*(\d+)\s+(.+)", line)
    if not match:
        return None
    ip = None
    for token in match.group(2).split():
        try:
            ip = str(ipaddress.ip_address(token.strip("()[]")))
            break
        except ValueError:
            pass
    values = [float(v.replace(",", ".")) for v in
              re.findall(r"<?\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)", match.group(2), re.I)]
    # tracert sends three probes per hop; use their mean RTT as a single cycle sample.
    latency = round(sum(values) / len(values), 3) if values else None
    return {"hop": int(match.group(1)), "ip": ip, "latency": latency,
            "status": "ok" if latency is not None else "timeout" if "*" in match.group(2) else "error",
            "probe_count": len(values), "detail": ""}


def route_command(ip, timeout, max_hops, system=None):
    system = system or platform.system()
    v6 = ipaddress.ip_address(ip).version == 6
    if system == "Windows":
        return ["tracert", "-6" if v6 else "-4", "-d", "-h", str(max_hops), "-w", str(int(timeout * 1000)), ip]
    executable = shutil.which("traceroute6" if v6 and system == "Darwin" else "traceroute")
    if not executable and system == "Darwin":
        # Finder launches apps without the user's shell PATH.
        executable = "/usr/sbin/traceroute6" if v6 else "/usr/sbin/traceroute"
    if not executable:
        raise FileNotFoundError("Не найдена системная утилита ICMP-трассировки")
    prefix = [executable] + (["-6"] if v6 and system != "Darwin" else [])
    return prefix + ["-I", "-n", "-m", str(max_hops), "-q", "1", "-w", str(max(1, math.ceil(timeout))), ip]


def trace_cycle(ip, timeout, max_hops, stop=None, on_hop=None, interface=None):
    if interface:return bound_trace(ip,timeout,max_hops,stop,on_hop,interface)
    stop = stop or threading.Event()
    result = {"hops": [], "destination": ip, "message": "", "method": "ICMP", "family": "ipv6" if ":" in ip else "ipv4"}
    started = time.time()
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if platform.system() == "Windows" else {}
    encoding = "oem" if platform.system() == "Windows" else "utf-8"
    command = route_command(ip, timeout, max_hops)
    done = threading.Event()
    # Windows tracert has 3 probes per hop. Missing hops can extend a cycle significantly.
    deadline = time.monotonic() + min(180, max_hops * max(1, timeout) * (3 if platform.system() == "Windows" else 1) + 8)
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding=encoding, errors="replace", bufsize=1, **kwargs) as process:
        def cancel_watch():
            while not done.wait(.1):
                if stop.is_set() or time.monotonic() > deadline:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
        watcher = threading.Thread(target=cancel_watch, daemon=True)
        watcher.start()
        try:
            for line in process.stdout:
                if stop.is_set():
                    break
                if any(s in line.lower() for s in ("not permitted", "permission denied", "must be root")):
                    result["message"] = "ОС запретила ICMP-трассировку"
                hop = parse_hop(line)
                if hop:
                    hop["ts"] = time.time()
                    if hop["status"] == "timeout":
                        hop["detail"] = "Узел не ответил на ICMP-зонд с этим TTL / Hop Limit"
                    result["hops"].append(hop)
                    if on_hop:
                        on_hop(hop)
            process.wait(timeout=2)
        finally:
            done.set()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
    reached = any(interface_probes.same_ip(h["ip"],ip) and h["status"] == "ok" for h in result["hops"])
    result["state"] = "complete" if reached else "partial" if result["hops"] else "error"
    result["message"] = result["message"] or ("Цель достигнута" if reached else "Маршрут неполный: цель не ответила, лимит узлов или фильтрация")
    result["cycle_seconds"] = round(time.time() - started, 3)
    return result


def bound_trace(ip,timeout,max_hops,stop,on_hop,identity):
    stop=stop or threading.Event();started=time.time();hops=[]
    binding=interface_probes.binding_for(identity,ip)
    for ttl in range(1,max_hops+1):
        if stop.is_set():break
        # Check the live interface before every TTL; disconnected adapters never fall back.
        binding=interface_probes.binding_for(identity,ip)
        response=interface_probes.echo(ip,binding,timeout,ttl,stop)
        if stop.is_set() or response['kind']=='cancelled':break
        good=response['kind'] in ('echo','ttl')
        hop={'hop':ttl,'ip':response['ip'],'latency':response['latency'] if good else None,
             'status':'ok' if good else 'timeout' if response['kind']=='timeout' else 'error',
             'probe_count':1 if good else 0,'ts':time.time(),
             'detail':interface_probes.detail(binding,response['kind'])}
        hops.append(hop)
        if on_hop:on_hop(hop)
        if response['kind'] in ('echo','unreachable'):break
    reached=bool(hops and interface_probes.same_ip(hops[-1]['ip'],ip) and hops[-1]['status']=='ok')
    return {'hops':hops,'destination':ip,'interface':identity,'source':binding['source'],
            'method':'ICMP bound','family':'ipv6' if ':' in ip else 'ipv4',
            'state':'complete' if reached else 'partial' if hops else 'error',
            'message':'Цель достигнута' if reached else 'Маршрут неполный через выбранный адаптер',
            'cycle_seconds':round(time.time()-started,3)}
