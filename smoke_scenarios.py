"""Isolated packaged-app acceptance. Invoked only with --smoke-test --smoke-scenario."""
import json,time
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QFileDialog

def start(window,folder,report,stream_report=False):
    w=window;app=QApplication.instance();app.setQuitOnLastWindowClosed(False)
    state={'stage':0,'began':time.monotonic(),'stage_at':time.monotonic(),'checks':[],'profile':str(folder),'version':w.monitor.snapshot(w.sid,compact=True)['version']}
    path=Path(report);export=Path(folder)/'acceptance.txt';initial=w.sid;identity=w.selected_interface
    def write():path.write_text(json.dumps({k:v for k,v in state.items() if k not in ('began','stage_at')},ensure_ascii=False,indent=2))
    def advance(label):
        state['checks'].append(label);state['stage']+=1;state['stage_at']=time.monotonic();write()
    def count():return w.store.rows('SELECT count(*) AS n FROM samples WHERE session=?',(w.sid,))[0]['n']
    def close():
        timer.stop();w.grab().save(str(path.with_suffix('.png')));w.close()
        def finished():
            if not w.closed_clean:QTimer.singleShot(100,finished);return
            state['closed_clean']=True;state['rows_after_close']={table:w.store.rows('SELECT count(*) AS n FROM '+table)[0]['n'] for table in ('sessions','samples','hop_samples','routes','events','session_data')}
            state['ok']=not state.get('error') and not any(state['rows_after_close'].values());write();app.quit()
        finished()
    def tick():
        try:
            if time.monotonic()-state['stage_at']>45:raise AssertionError('Превышено время этапа '+str(state['stage']))
            snap=w.monitor.snapshot(w.sid,compact=True) if w.sid else {};target=(snap.get('targets') or [{}])[0];last=target.get('last') or {};stage=state['stage']
            if stage==0 and target.get('window_stats',{}).get('ok',0)>=3:
                assert identity and identity in last.get('detail','')
                state['gateway_ok']=target['window_stats']['ok'];w.toggle();state['pause_count']=count();advance('Ручной адаптер: ответы шлюза; пауза')
            elif stage==1 and time.monotonic()-state['stage_at']>1.2:
                assert w.paused and w.sid==initial and count()==state['pause_count'];w.toggle();advance('Пауза сохраняет историю, пробы не добавляются')
            elif stage==2 and count()>state['pause_count']+2:
                assert w.sid==initial;w.nic.addItem('Test unavailable','en999');w.nic.setCurrentIndex(w.nic.findData('en999'));state['unavailable_since']=time.time();advance('Продолжение той же сессии; выбран отсутствующий адаптер')
            elif stage==3 and last.get('ts',0)>state['unavailable_since'] and last.get('status')=='error':
                rows=w.store.rows("SELECT status FROM samples WHERE session=? AND ts>=?",(w.sid,state['unavailable_since']))
                assert not any(row['status']=='ok' for row in rows);w.nic.setCurrentIndex(w.nic.findData(identity));state['returned_since']=time.time();advance('Нет fallback: отсутствующий адаптер даёт ошибку')
            elif stage==4 and last.get('ts',0)>state['returned_since'] and last.get('status')=='ok':
                assert identity in last.get('detail','');w.address.setText('ya.ru');w.toggle();advance('Возврат исходного адаптера восстанавливает ping')
            elif stage==5 and w.sid!=initial and target.get('host')=='ya.ru' and target.get('window_stats',{}).get('ok',0)>=3:
                assert target['window_stats']['count']==count();state['new_session_ok']=target['window_stats']['ok']
                w.period.setCurrentIndex(w.period.findData(43200));w.render(w.monitor.snapshot(w.sid,43200,compact=True))
                expected=next(t for t in w.snapshot['targets'] if t['mode']=='icmp')['window_last']
                assert w.route_summary.current==expected['latency']
                with patch.object(QFileDialog,'getSaveFileName',return_value=(str(export),'Текст')):w.export_session()
                advance('Новый сервер — новая сессия; масштаб не искажает Cur; экспорт начат')
            elif stage==6 and w.export_worker and not w.export_worker.isRunning() and export.exists():
                doc=json.loads(export.read_text().split('\n\n',1)[1]);assert doc['session']['id']==w.sid and doc['samples']
                stats=next(iter(doc['statistics'].values()));assert stats['jitter']==doc['derived_samples'][-1]['jitter']
                state['export_bytes']=export.stat().st_size;advance('Асинхронный экспорт согласован с исходными пробами')
                if not stream_report:close()
            elif stage==7 and stream_report:
                panel=w.stream_panel;now=time.time()
                with panel.monitor.lock:
                    panel.monitor.started=now-5000;panel.monitor.events.clear()
                    panel.monitor.events.extend(dict(ts=now-5000+i,key='',severity='info',message='Учебное событие '+str(i)) for i in range(5000))
                w.tabs.setCurrentIndex(1);panel.period.setCurrentIndex(panel.period.findData(43200));panel.refresh()
                state['report_beats']=[];heartbeat=QTimer(w);heartbeat.timeout.connect(lambda:state['report_beats'].append(time.monotonic()));heartbeat.start(20);w.report_heartbeat=heartbeat
                tick=time.perf_counter();panel.start_report(str(Path(folder)/'stream-report.txt'))
                state['stream_report_dispatch_ms']=round((time.perf_counter()-tick)*1000,3)
                advance('Учебный Stream: 5000 событий, подготовка TXT в фоне')
            elif stage==8 and stream_report and not w.stream_panel.report_pending:
                target=Path(folder)/'stream-report.txt';assert target.exists()
                text=target.read_text(encoding='utf-8');assert 'Учебное событие 4999' in text
                w.report_heartbeat.stop();beats=state.pop('report_beats');gaps=[b-a for a,b in zip(beats,beats[1:])]
                state['stream_report_heartbeat_max_ms']=round(max(gaps,default=0)*1000,3)
                assert state['stream_report_dispatch_ms']<200
                state.update(stream_report_verified=True,synthetic_stream_events=5000,stream_report_bytes=target.stat().st_size)
                advance('Stream TXT готов; окно отвечало во время подготовки');close()
        except Exception as exc:state['error']=type(exc).__name__+': '+str(exc);write();close()
    timer=QTimer(w);timer.timeout.connect(tick);timer.start(150);w.acceptance_timer=timer;write()
