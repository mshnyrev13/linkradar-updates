"""Stream tab engine: polls stream sources, keeps RAM history and builds the provider report.

History lives only in memory (at most 12 hours) and is cleared on stop/close.
Thresholds mark values for attention; they do not prove viewer-side quality.
"""
import ipaddress
import os
import socket
import sys
import threading
import time
from collections import deque
from time_labels import format_timestamp
import error_log
import stream_sources as sources

HISTORY_SECONDS=12*3600
# Attention levels: OBS draws congestion from green to red over 0..1; dropped/skipped
# frames above ~1% are visible to viewers; TCP RTT/retransmissions describe the path.
LIMITS={'drop_pct':(.1,1),'congestion':(.1,.5),'rtt':(150,300),'retrans_pct':(1,3)}
SECTIONS=('obs','vmix_api','vmix_stats','tcp','ndi')
UNSET=object()
CLOSED=object()
STATE_TEXT={'live':'в эфире','reconnecting':'переподключение','connected':'подключено','found':'обнаружен','off':'остановлен','lost':'пропал'}

def level(metric,value):
    if value is None:return 0
    warn,bad=LIMITS[metric]
    return 2 if value>=bad else 1 if value>=warn else 0

def rate(previous,current,key,seconds):
    """kbit/s from a byte counter; None on the first sample or a counter reset."""
    if not previous or seconds<=0 or current.get(key) is None or previous.get(key) is None or current[key]<previous[key]:return None
    return (current[key]-previous[key])*8/1000/seconds

def fmt(value,digits=0):
    return '—' if value is None else f'{value:.{digits}f}'.replace('.',',')

def is_public(endpoint):
    """True for a global IP in 'ip:port' or '[ip]:port'."""
    host=(endpoint or '').rsplit(':',1)[0].strip('[]')
    try:return ipaddress.ip_address(host).is_global
    except ValueError:return False

def duration(seconds):
    if seconds is None:return '—'
    seconds=int(seconds);return f'{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}'


def summarize(points):
    """Statistics for a captured, time-bounded history; no monitor lock or I/O."""
    result={'samples':len(points)}
    for metric in ('bitrate','rtt','congestion','retrans_pct','fps'):
        values=[p[1][metric] for p in points if p[1].get(metric) is not None]
        result[metric]={'avg':sum(values)/len(values),'min':min(values),'max':max(values)} if values else None
    result['dropped']=sum(p[1].get('dropped_delta') or 0 for p in points)
    result['live_seconds']=sum(min(2,max(0,b[0]-a[0])) for a,b in zip(points,points[1:]) if a[2] in ('live','connected'))
    return result


class StreamMonitor:
    def __init__(self,clock=time.time,history_seconds=HISTORY_SECONDS):
        self.clock=clock;self.history_seconds=history_seconds;self.lock=threading.RLock()
        self.thread=None;self.stop_event=threading.Event();self.obs=None;self.obs_reset=False;self.jobs={};self.generation=0
        self.settings={'obs_host':'127.0.0.1','obs_port':None,'obs_password':'','vmix_url':sources.VMIX_API,'ndi':True,'tcp':True}
        self.reset()
    def reset(self):
        with self.lock:
            self.status={key:{'state':'off','text':'не опрашивается'} for key in SECTIONS}
            self.streams={};self.history={};self.events=deque(maxlen=5000);self.vmix_rows=[];self.ndi=[];self.obs_engine={};self.vmix_info={}
            self.counters={};self.vmix_sample_at=None;self.started=None;self.last_poll=None;self.cycle_now=None;self.obs_seen=set();self.vmix_seen=set();self.ndi_seen=set();self.stats_seen=set();self.tcp_seen=set();self.source_times={}
            # Results of slow reads that are still running belong to the old generation.
            self.generation+=1;self.jobs={}
    @property
    def running(self):return bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set())
    def configure(self,**values):
        with self.lock:
            changed={key for key,value in values.items() if self.settings.get(key)!=value};self.settings.update(values)
            if changed&{'obs_host','obs_port','obs_password'}:self.obs_reset=True
            if changed:
                self.generation+=1
                for job in self.jobs.values():job.pop('result',None);job['due']=0
                self.counters={};self.vmix_sample_at=None
    def start(self):
        if self.thread and self.thread.is_alive():return False
        self.stop_event=threading.Event()
        with self.lock:
            if self.started is None:self.started=self.clock()
            self.event('','info','Мониторинг трансляций запущен')
        self.thread=threading.Thread(target=self.run,args=(self.stop_event,),name='stream-monitor',daemon=True);self.thread.start()
    def pause(self):
        self.stop_event.set()
        if self.thread:self.thread.join(0)
        with self.lock:
            self.obs_reset=True
            # Rate baselines must restart, while already observed reconnect
            # segments must not disappear when polling resumes.
            self.counters={key:{'carry':dict(value['carry'])} for key,value in self.counters.items() if 'carry' in value}
            self.vmix_sample_at=None
            for stream in self.streams.values():stream['observation_gap']=True
            self.generation+=1
            for job in self.jobs.values():job.pop('result',None);job['due']=0
            self.event('','info','Мониторинг трансляций приостановлен')
    def clear(self):
        self.stop_event.set()
        if self.thread:self.thread.join(5)
        if self.thread and self.thread.is_alive():raise RuntimeError('Опрос Stream ещё завершается; очистка не выполнена')
        with self.lock:self.obs_reset=True
        self.thread=None;self.reset()
    def run(self,stop=None):
        stop=stop or self.stop_event
        while not stop.is_set():
            began=time.monotonic()
            try:self.cycle()
            except Exception as exc:
                error_log.error('Внутренняя ошибка опроса Stream',exc,key='stream-cycle')
                with self.lock:self.event('','error','Внутренняя ошибка опроса: '+type(exc).__name__)
            stop.wait(max(.2,1-(time.monotonic()-began)))
        job=self.jobs.get('obs')
        if job and job.get('thread'):job['thread'].join()
        if self.obs:self.obs.close();self.obs=None
    # --- collection ---------------------------------------------------------
    def fetch(self,name,function,interval,now):
        """Latest result of a slow read running in its own thread; None while nothing new.

        A refused localhost connection takes about 2 s on Windows and mDNS waits 1.5 s,
        so these reads must not stall the one-second poll of the other sources.
        """
        with self.lock:
            job=self.jobs.get(name)
            if job and job['thread'].is_alive():return None
            result=job.pop('result',None) if job else None
            if not job or now>=job['due']:
                job=self.jobs[name]={'due':now+interval};generation=self.generation
                def work():
                    value=self.read(function)
                    with self.lock:
                        if self.generation==generation and self.jobs.get(name) is job:job['result']=value
                job['thread']=threading.Thread(target=work,name='stream-'+name,daemon=True);job['thread'].start()
            return result
    def cycle(self,now=None,obs=UNSET,vmix_api=UNSET,vmix_rows=UNSET,connections=UNSET,ndi=UNSET):
        """One poll. Tests pass source results directly; UNSET reads the real source."""
        now=now or self.clock();seen=set();updated=set()
        settings=dict(self.settings)
        if obs is UNSET:obs=self.fetch('obs',self.read_obs,1,now)
        if vmix_api is UNSET:vmix_api=self.fetch('vmix_api',lambda:sources.vmix_api(settings['vmix_url']),2,now)
        if ndi is UNSET:ndi=self.fetch('ndi',sources.discover_ndi,15,now) if settings['ndi'] else None
        if vmix_rows is UNSET:vmix_rows=self.fetch('vmix_stats',lambda:sources.vmix_statistics() or CLOSED,1,now)
        else:
            if vmix_rows is None:vmix_rows=CLOSED
        with self.lock:
            # Report period starts at the first poll even if cycle() is driven without start().
            if self.started is None:self.started=now
            self.cycle_now=now
            if ndi is not None:self.ndi_seen=self.update_ndi(now,ndi);self.source_times['ndi']=now;updated|=self.ndi_seen
            endpoints={(item['ip'],item['port']) for item in self.ndi if item.get('ip')}
        if connections is UNSET:connections=self.fetch('tcp',lambda:sources.encoder_connections(endpoints,exclude_pids={os.getpid()}),1,now) if settings['tcp'] else None
        with self.lock:
            self.last_poll=now
            # Sources without a new result keep their streams visible but add no samples.
            if obs is not None:self.obs_seen=self.update_obs(now,obs);updated|=self.obs_seen;self.source_times['obs']=now
            if vmix_api is not None:self.vmix_seen=self.update_vmix_api(now,vmix_api);updated|=self.vmix_seen;self.source_times['vmix_api']=now
            if vmix_rows is not None:
                self.stats_seen=self.update_vmix_rows(now,None if vmix_rows is CLOSED else vmix_rows);updated|=self.stats_seen;self.source_times['vmix_stats']=now
            if connections is not None:
                self.tcp_seen=self.update_tcp(now,connections);updated|=self.tcp_seen;self.source_times['tcp']=now
            for section,attr,timeout in (('obs','obs_seen',15),('vmix_api','vmix_seen',15),('vmix_stats','stats_seen',15),('tcp','tcp_seen',15),('ndi','ndi_seen',45)):
                if now-self.source_times.get(section,now)>timeout:
                    setattr(self,attr,set());self.status[section]={'state':'error','text':'нет свежих данных'}
                seen|=getattr(self,attr)
            seen|=updated
            self.link_tcp()
            for key,stream in self.streams.items():
                if key not in seen and stream['state'] not in ('off','lost'):
                    # Short TCP connections (API/HTTPS calls) are not stream incidents.
                    if not key.startswith('tcp:') or now-stream['first_seen']>=30:self.event(key,'warn',stream['label']+': источник больше не виден')
                    stream['state']='lost';stream['active']=False;stream['bitrate']=None;stream['rtt']=None;stream['retrans_pct']=None
                if key in updated:self.sample(key,now)
            self.prune(now);self.cycle_now=None
    def read(self,function):
        try:return function()
        except sources.SourceError as exc:return exc
        except OSError as exc:return sources.SourceError(str(exc) or type(exc).__name__)
        except Exception as exc:
            error_log.error('Ошибка источника Stream',exc,key='stream-source')
            return sources.SourceError('Источник вернул некорректные данные: '+type(exc).__name__)
    def read_obs(self):
        """Runs in the OBS worker thread; the client is used by that thread only."""
        if self.obs_reset and self.obs:self.obs.close();self.obs=None
        self.obs_reset=False
        if not self.obs:
            settings=self.settings;self.obs=sources.ObsClient(settings['obs_host'],settings['obs_port'],settings['obs_password'])
        return self.obs.poll()
    def set_status(self,section,result,ok_text):
        if isinstance(result,Exception):
            previous=self.status[section]
            if previous['state']=='ok':self.event('','warn',{'obs':'OBS','vmix_api':'vMix API','vmix_stats':'vMix Statistics','tcp':'TCP','ndi':'NDI'}[section]+': '+sources.safe_text(result))
            self.status[section]={'state':'error','text':sources.safe_text(result)};return False
        self.status[section]={'state':'ok','text':ok_text};return True
    def stream(self,key,now,**info):
        stream=self.streams.get(key)
        if stream is None:
            stream=self.streams[key]={'key':key,'state':'off','first_seen':now,'reconnects':0,'live_since':None}
            self.history[key]=deque()
        state=info.pop('state',stream['state']);previous=stream['state'];stream.update(info);stream['last_seen']=now
        if 'label' not in stream:stream['label']=key
        if state!=previous:
            stream['state']=state
            if key.startswith('tcp:'):return stream
            if state=='live':stream['live_since']=now
            if state=='reconnecting':stream['reconnects']+=1
            if previous!='off' or state not in ('off','found'):
                self.event(key,'bad' if state=='reconnecting' else 'warn' if previous in ('live','reconnecting','connected') and state not in ('live','connected') else 'info',stream['label']+': '+STATE_TEXT.get(state,state))
        return stream
    def counter_delta(self,key,name,value):
        """Growth of a cumulative counter; a decrease means a restart, not negative loss."""
        store=self.counters.setdefault(key,{});old=store.get(name);store[name]=value
        if value is None or old is None or value<old:return None
        return value-old
    def update_obs(self,now,data):
        if not self.set_status('obs',data,'подключено'):return set()
        version=data.get('version') or {};stats=data.get('stats') or {};video=data.get('video') or {}
        fps=(video.get('fpsNumerator') or 0)/(video.get('fpsDenominator') or 1) if video.get('fpsNumerator') else None
        self.status['obs']['text']=f"OBS {version.get('obsVersion','?')} · WebSocket {version.get('obsWebSocketVersion','?')}"
        render=self.counter_delta('obs','render_skipped',stats.get('renderSkippedFrames'));encoder=self.counter_delta('obs','encoder_skipped',stats.get('outputSkippedFrames'))
        self.obs_engine={'cpu':stats.get('cpuUsage'),'memory':stats.get('memoryUsage'),'active_fps':stats.get('activeFps'),'render_ms':stats.get('averageFrameRenderTime'),
            'render_skipped':stats.get('renderSkippedFrames'),'render_total':stats.get('renderTotalFrames'),'encoder_skipped':stats.get('outputSkippedFrames'),
            'encoder_total':stats.get('outputTotalFrames'),'disk_mb':stats.get('availableDiskSpace'),'fps':fps,
            'base':f"{video.get('baseWidth')}x{video.get('baseHeight')}" if video.get('baseWidth') else None,
            'output':f"{video.get('outputWidth')}x{video.get('outputHeight')}" if video.get('outputWidth') else None}
        if render:self.event('obs','warn',f'OBS: {render} кадр(ов) пропущено рендером (нагрузка GPU/сцены)')
        if encoder:self.event('obs','warn',f'OBS: {encoder} кадр(ов) пропущено кодировщиком (нагрузка CPU/GPU)')
        seen=set();service=data.get('service') or {}
        outputs=[('obs:stream','OBS · Трансляция',data.get('stream') or {},service.get('protocol') or 'RTMP',sources.safe_url(service.get('server') or '')),
                 ('obs:record','OBS · Запись',data.get('record') or {},'Файл','')]
        outputs+=[('obs:out:'+o['name'],'OBS · '+o['name'],o,{'virtualcam_output':'Камера'}.get(o.get('kind'),'NDI' if 'ndi' in (o.get('kind') or o['name']).lower() else o.get('kind') or 'Выход'),'') for o in data.get('outputs',[])]
        for key,label,status,protocol,target in outputs:
            active=bool(status.get('outputActive'));state='reconnecting' if status.get('outputReconnecting') else 'live' if active else 'off'
            if key!='obs:stream' and not active and key not in self.streams:continue
            before=self.streams.get(key,{}).get('state')
            stream=self.stream(key,now,label=label,source='OBS',protocol=protocol,target=target,state=state)
            current={'bytes':status.get('outputBytes'),'skipped':status.get('outputSkippedFrames'),'total':status.get('outputTotalFrames'),
                     'duration':(status.get('outputDuration') or 0)/1000,'active':active,'ts':now}
            store=self.counters.setdefault(key,{});previous=store.get('sample');store['sample']=current
            # OBS restarts bytes, frames and duration after a reconnect (checked on Twitch, OBS 32).
            # Totals of one broadcast carry over; a new broadcast starts from zero.
            if previous and not previous['active']:
                store['carry']={'skipped':0,'total':0,'duration':0};stream['observation_gap']=False
            carry=store.setdefault('carry',{'skipped':0,'total':0,'duration':0})
            if previous and previous['active'] and active:
                for name in carry:
                    if current[name] is not None and previous[name] is not None and current[name]<previous[name]:carry[name]+=previous[name]
                # A reconnect shorter than the poll is seen only as the byte counter reset.
                if current['bytes'] is not None and previous['bytes'] is not None and current['bytes']<previous['bytes'] and 'reconnecting' not in (before,state):
                    stream['reconnects']+=1;self.event(key,'bad',label+': переподключение (счётчики OBS начались заново)')
            seconds=now-previous['ts'] if previous else 0
            dropped=None if not previous or current['skipped'] is None or previous['skipped'] is None or current['skipped']<previous['skipped'] else current['skipped']-previous['skipped']
            total=current['total']+carry['total'] if current['total'] is not None else None
            skipped=current['skipped']+carry['skipped'] if current['skipped'] is not None else None
            stream.update(active=active,bitrate=rate(previous,current,'bytes',seconds) if active else None,fps=self.obs_engine['active_fps'] if active else None,
                frames_total=total,frames_dropped=skipped,dropped_delta=dropped if active else None,
                dropped_rate=dropped/seconds if active and dropped is not None and seconds>0 else None,
                drop_pct=100*skipped/total if total and skipped is not None else None,
                congestion=status.get('outputCongestion') if active else None,duration=current['duration']+carry['duration'] if active else None,
                bytes=current['bytes'],paused=status.get('outputPaused'),
                note='Потери: сеть (Dropped Frames Network)' if key=='obs:stream' else '')
            # The last broadcast's air time stays in the report after the stop.
            if active:stream['aired']=stream['duration']
            if dropped:self.event(key,'bad',f'{label}: потеряно {dropped} кадр(ов) при передаче')
            congestion=stream.get('congestion')
            if level('congestion',congestion)==2 and level('congestion',stream.get('last_congestion'))<2:self.event(key,'bad',f'{label}: перегрузка канала (congestion {fmt(congestion,2)})')
            stream['last_congestion']=congestion;seen.add(key)
        return seen
    def update_vmix_api(self,now,data):
        if not self.set_status('vmix_api',data,'подключено'):return set()
        seen=set();self.status['vmix_api']['text']=f"vMix {data.get('version') or '?'} {data.get('edition') or ''}".strip()
        self.vmix_info=data
        for key,label,flag,protocol in (('vmix:stream','vMix · Трансляция','streaming','RTMP/SRT'),('vmix:record','vMix · Запись','recording','Файл'),
                                        ('vmix:external','vMix · External','external','Выход'),('vmix:multicorder','vMix · MultiCorder','multicorder','Файл')):
            if not data.get(flag) and key not in self.streams:continue
            note='каналы: '+', '.join(data.get('streaming_channels',[])) if flag=='streaming' and data.get('streaming_channels') else ''
            self.stream(key,now,label=label,source='vMix',protocol=protocol,target='',state='live' if data.get(flag) else 'off',active=bool(data.get(flag)),note=note);seen.add(key)
        for item in data.get('inputs',[]):
            if not sources.STREAM_INPUT.search(item['type']):continue
            key='vmix:input:'+str(item['number']);seen.add(key)
            self.stream(key,now,label='vMix · '+(item['title'] or item['type']),source='vMix',protocol=item['type'],
                        target='вход '+str(item['number']),state='connected',active=True,note='состояние входа: '+item['state'] if item['state'] else '')
        return seen
    def update_vmix_rows(self,now,rows):
        if isinstance(rows,Exception):self.set_status('vmix_stats',rows,'');return set()
        if rows is None:
            self.vmix_sample_at=None
            self.status['vmix_stats']={'state':'off','text':'окно Statistics закрыто'};return set()
        # Statistics names inputs by title; a renamed SRT/NDI input is recognised by its API type.
        try:local_api=sources.is_loopback(sources.urllib.parse.urlsplit(self.settings['vmix_url']).hostname)
        except ValueError:local_api=False
        types={item['title']:item['type'].upper() for item in self.vmix_info.get('inputs',[]) if item.get('title')} if local_api else {}
        for row in rows:
            if row['kind']=='input' and types.get(row['name']) in ('SRT','NDI'):row['kind']={'SRT':'srt_input','NDI':'ndi'}[types[row['name']]]
        seconds=now-self.vmix_sample_at if self.vmix_sample_at is not None else 0
        sources.vmix_deltas(rows,self.vmix_rows if self.vmix_sample_at is not None and seconds>=0 else []);self.vmix_rows=rows;self.vmix_sample_at=now
        self.set_status('vmix_stats',rows,f'{len(rows)} строк')
        seen=set()
        for row in rows:
            delta=row.get('delta',{});row['interval_seconds']=seconds if seconds>0 else None
            lost_keys=('source_dropped','renderer_dropped','fullscreen_dropped','audio_dropped')
            lost=sum(delta.get(k,0) for k in lost_keys) if any(k in delta for k in lost_keys) else None
            if lost:self.event('vmix:stat:'+row['name'],'bad',f"vMix {row['name']}: +{lost} потерь ("+', '.join(f'{k} +{v}' for k,v in delta.items() if v)+')')
            if row['kind']=='srt':
                # Packet loss is the SRT link's own figure, not TCP retransmission.
                key='vmix:srt:'+row['name'];seen.add(key);loss=row.get('packet_loss_pct')
                self.stream(key,now,label='vMix · SRT '+row['name'],source='vMix Statistics',protocol='SRT',target='',state='connected',active=True,
                            bitrate=row.get('bandwidth_kbps'),rtt=row.get('rtt_ms'),packet_loss_pct=loss,
                            note=' · '.join(p for p in ('потери пакетов '+f'{loss:g} %' if loss is not None else '','длительность '+row['duration'] if row.get('duration') else '') if p))
            elif row['kind'] in ('srt_input','srt_output','output','ndi'):
                key='vmix:stat:'+row['name'];seen.add(key)
                self.stream(key,now,label='vMix · '+row['name'],source='vMix Statistics',protocol={'srt_input':'SRT вход','srt_output':'SRT выход','output':'Выход','ndi':'NDI'}[row['kind']],
                            target=row.get('endpoint',''),state='connected',active=True,dropped_delta=lost,
                            dropped_rate=lost/seconds if lost is not None and seconds>0 else None,
                            frames_dropped=sum(row.get(k) or 0 for k in ('source_dropped','renderer_dropped','fullscreen_dropped')),latency=row.get('latency_ms'),
                            note=('аудиобуфер '+str(row['audio_buffer_ms'])+' мс' if row.get('audio_buffer_ms') is not None else ''))
        return seen
    def update_tcp(self,now,connections):
        if not self.set_status('tcp',connections,''):return set()
        live={app for key,app in (('obs:stream','OBS'),('vmix:stream','vMix')) if self.streams.get(key,{}).get('state') in ('live','reconnecting')}
        # HTTPS/other ports of OBS/vMix are API/update traffic unless the app is on air.
        connections=[c for c in connections if c['protocol'] in ('RTMP','NDI','RTSP') or c['app'] not in ('OBS','vMix') or c['app'] in live]
        with_stats=[c for c in connections if c.get('estats')]
        self.status['tcp']['text']=f'{len(connections)} потоковых соединений энкодеров'+('' if with_stats or not connections else ' · байты/RTT: нужен запуск от администратора')
        seen=set()
        for conn in connections:
            key=f"tcp:{conn['app']}:{conn['remote']}:{conn['remote_port']}:{conn['local_port']}";seen.add(key);estats=conn.get('estats') or {}
            stream=self.stream(key,now,label=f"{conn['app']} · TCP {conn['protocol']}",source=conn['app']+' · TCP',protocol=conn['protocol'],
                               target=f"{conn['remote']}:{conn['remote_port']}",state='connected',active=True,process=conn['process'])
            current={'bytes':estats.get('bytes_out'),'segments':estats.get('segments_out'),'retrans':estats.get('retrans_segments'),'ts':now}
            previous=self.counters.get(key,{}).get('sample');self.counters.setdefault(key,{})['sample']=current
            seconds=now-previous['ts'] if previous else 0;sent=None
            if previous and current['segments'] is not None and previous['segments'] is not None and current['segments']>=previous['segments']:sent=current['segments']-previous['segments']
            retrans=self.counter_delta(key,'retrans',current['retrans'])
            stream.update(bitrate=rate(previous,current,'bytes',seconds),rtt=estats.get('rtt'),bytes=current['bytes'],retrans_total=current['retrans'],
                          retrans_pct=100*retrans/sent if sent and retrans is not None else None,
                          note='' if estats else 'байты/RTT: нужен администратор')
            if level('retrans_pct',stream['retrans_pct'])==2:self.event(key,'warn',f"{stream['label']}: ретрансмиты {fmt(stream['retrans_pct'],1)}%")
        return seen
    def update_ndi(self,now,found):
        if not self.set_status('ndi',found,''):return set()
        self.ndi=found;self.status['ndi']['text']=f'{len(found)} источников в сети (mDNS)'
        for item in found:
            self.stream('ndi:'+item['name'],now,label='NDI · '+item['name'],source='NDI',protocol='NDI',target=f"{item.get('ip') or item.get('host')}:{item.get('port')}",state='found',active=False)
        return {'ndi:'+item['name'] for item in found}
    def link_tcp(self):
        """Attach the busiest matching encoder connection's RTT/bitrate to OBS/vMix streams."""
        for key,app in (('obs:stream','OBS'),('vmix:stream','vMix')):
            stream=self.streams.get(key)
            if not stream or stream['state'] not in ('live','reconnecting'):continue
            try:local=sources.is_loopback(self.settings['obs_host']) if app=='OBS' else sources.is_loopback(sources.urllib.parse.urlsplit(self.settings['vmix_url']).hostname)
            except ValueError:local=False
            if not local:
                stream.pop('tcp_target',None);stream['rtt']=None;stream['retrans_pct']=None;continue
            candidates=[s for k,s in self.streams.items() if k.startswith('tcp:'+app+':') and s['state']=='connected' and s['protocol'] in ('RTMP','RTMPS/HTTPS')]
            if not candidates:
                stream.pop('tcp_target',None);stream['rtt']=None;stream['retrans_pct']=None;continue
            best=max(candidates,key=lambda s:s.get('bitrate') or 0)
            stream['rtt']=best.get('rtt');stream['retrans_pct']=best.get('retrans_pct');stream['tcp_target']=best['target']
            if stream.get('bitrate') is None and best.get('bitrate'):stream['bitrate']=best['bitrate']
    def sample(self,key,now):
        stream=self.streams[key]
        self.history[key].append((now,{k:stream.get(k) for k in ('bitrate','dropped_delta','dropped_rate','rtt','congestion','retrans_pct','fps')},stream['state']))
    def prune(self,now):
        limit=now-self.history_seconds
        for key in list(self.history):
            points=self.history[key]
            while points and points[0][0]<limit:points.popleft()
            if not points and self.streams[key].get('last_seen',now)<limit:del self.history[key];del self.streams[key];self.counters.pop(key,None)
        while self.events and self.events[0]['ts']<limit:self.events.popleft()
    def event(self,key,severity,message):
        self.events.append({'ts':self.clock() if self.cycle_now is None else self.cycle_now,'key':key,'severity':severity,'message':message})
    # --- reading ------------------------------------------------------------
    def summary(self,key,period,now=None):
        now=self.clock() if now is None else now
        with self.lock:points=[p for p in self.history.get(key,()) if now-min(period,self.history_seconds)<=p[0]<=now]
        return summarize(points)
    def snapshot(self,period=300,key=None):
        """Current state; history only for the selected stream to keep refresh cheap."""
        with self.lock:
            now=self.clock();self.prune(now)
            return {'running':self.running,'status':{k:dict(v) for k,v in self.status.items()},'streams':[dict(s) for s in self.streams.values()],
                    'history':{k:[p for p in v if now-min(period,self.history_seconds)<=p[0]<=now] for k,v in self.history.items() if k==key},'events':[e for e in self.events if e['ts']<=now],
                    'vmix_rows':[dict(r) for r in self.vmix_rows],'ndi':list(self.ndi),'obs':dict(self.obs_engine),'now':now,'started':self.started}
    def report(self,period=3600,network=None,now=None):
        """Plain-text report for a CDN/video service. No stream keys or passwords."""
        with self.lock:
            now=self.clock() if now is None else now;period=min(period,self.history_seconds)
            streams=sorted((dict(s) for s in self.streams.values()),key=lambda s:s['key']);events=[e for e in self.events if now-period<=e['ts']<=now]
            # Freeze rows with the metadata/events. Formatting must not reread
            # histories while the measurement thread is adding later probes.
            histories={s['key']:tuple(p for p in self.history.get(s['key'],()) if now-period<=p[0]<=now) for s in streams}
            status={k:dict(v) for k,v in self.status.items()};obs=dict(self.obs_engine);started=self.started;vmix=dict(self.vmix_info)
        local=lambda ts:format_timestamp(ts,'%Y-%m-%d %H:%M:%S');utc=lambda ts:format_timestamp(ts,utc=True)
        offset=time.strftime('%z',time.localtime(now));begin=max(now-period,started or now)
        lines=['LinkRadar · отчёт о трансляции',
               f'Сформирован: {local(now)} (UTC{offset[:3]}:{offset[3:]}) · {time.strftime("%Y-%m-%d %H:%M:%S",time.gmtime(now))} UTC',
               f'Период: {local(begin)} — {local(now)} · {duration(now-begin)}',
               'Компьютер: '+socket.gethostname()+' · '+sys.platform,'',
               'Источники данных:']
        names={'obs':'OBS','vmix_api':'vMix API','vmix_stats':'vMix Statistics','tcp':'TCP энкодеров','ndi':'NDI (mDNS)'}
        lines+=[f"  {names[k]}: {'OK' if v['state']=='ok' else 'нет данных'} · {v['text']}" for k,v in status.items()]
        if obs:
            lines+=['','Кодировщик OBS:',
                    f"  Холст {obs.get('base') or '—'} → вывод {obs.get('output') or '—'} · {fmt(obs.get('fps'),2)} fps (факт {fmt(obs.get('active_fps'),1)})",
                    f"  CPU {fmt(obs.get('cpu'),1)}% · рендер кадра {fmt(obs.get('render_ms'),2)} мс",
                    f"  Пропущено рендером: {obs.get('render_skipped')} из {obs.get('render_total')} · кодировщиком: {obs.get('encoder_skipped')} из {obs.get('encoder_total')}"]
        if vmix:lines+=['',f"vMix: {vmix.get('version') or '?'} {vmix.get('edition') or ''} · трансляция {'да' if vmix.get('streaming') else 'нет'} · запись {'да' if vmix.get('recording') else 'нет'}"]
        lines+=['','Потоки:']
        shown=[s for s in streams if s['state']!='found' and not (s['key'].startswith('tcp:') and not s.get('bytes'))]
        ndi=[s for s in streams if s['key'].startswith('ndi:')]
        if not shown:lines.append('  нет обнаруженных потоков')
        for s in shown:
            stat=summarize(histories[s['key']])
            def triple(metric,digits=0,unit=''):
                value=stat[metric]
                return '—' if not value else f"сред {fmt(value['avg'],digits)} · мин {fmt(value['min'],digits)} · макс {fmt(value['max'],digits)}{unit}"
            lines+=[f"  [{s['label']}] {s.get('protocol') or ''} {s.get('target') or s.get('tcp_target') or ''}".rstrip(),
                    f"    Состояние: {STATE_TEXT.get(s['state'],s['state'])} · в эфире {duration(s.get('duration') if s.get('duration') is not None else s.get('aired'))} · переподключений {s.get('reconnects',0)}",
                    f"    Битрейт, кбит/с: {triple('bitrate')}"]
            if s.get('frames_total') is not None or s.get('frames_dropped') is not None:
                lines.append(f"    Кадры: всего {s.get('frames_total') if s.get('frames_total') is not None else '—'} · потеряно {s.get('frames_dropped') if s.get('frames_dropped') is not None else '—'} ({fmt(s.get('drop_pct'),2)}%) · за период {stat['dropped']}")
            if stat['congestion']:lines.append(f"    Congestion (0–1): {triple('congestion',2)}")
            if stat['rtt']:
                lines.append(f"    TCP RTT, мс: {triple('rtt')} · ретрансмиты: {triple('retrans_pct',2,' %')}")
                if stat['rtt']['max']<=1 and is_public(s.get('tcp_target') or s.get('target')):lines.append('    RTT ≤1 мс до внешнего адреса: вероятно, локальный VPN/прокси, RTT и ретрансмиты — до него, а не до ingest')
            if s.get('latency') is not None:lines.append(f"    Задержка vMix: {s['latency']} мс")
            if s.get('note'):lines.append('    Примечание: '+s['note'])
            if s.get('observation_gap'):lines.append('    Наблюдение прерывалось: статистика содержит только известные сегменты; события во время паузы могли быть пропущены.')
        tcp=[s for s in streams if s['key'].startswith('tcp:') and not s.get('bytes')]
        if tcp:lines+=['','TCP-соединения энкодеров (без счётчиков ESTATS):']+[f"  {s['label']} → {s['target']} · {STATE_TEXT.get(s['state'],s['state'])}" for s in tcp]
        if ndi:lines+=['','NDI в сети: '+', '.join(s['label'][6:]+' ('+s['target']+')' for s in ndi)]
        if network:lines+=['','Сеть (вкладка «Сеть»):']+['  '+line for line in network]
        lines+=['','События (местное время / UTC):']
        lines+=[f"  {local(e['ts'])} / {utc(e['ts'])} UTC · {e['message']}" for e in events] or ['  нет']
        lines+=['','Данные получены только чтением OBS/vMix и таблиц ОС. Ключи потоков, пароли и параметры SRT не включены.',
                'Пороги внимания: потери кадров ≥0,1%/1%, congestion ≥0,1/0,5, RTT ≥150/300 мс, ретрансмиты ≥1%/3%.']
        return '\n'.join(lines)+'\n'
