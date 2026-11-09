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


def feedback(window,folder,report_path):
    """Disposable packaged-app regression with synthetic fields and native cwd.

    Only called with --smoke-test --smoke-feedback. Dialog answers and clipboard
    are controlled; the user's clipboard, installed profile and permissions are
    untouched. The export itself uses the actual workers and session database.
    """
    import errno
    import os
    from unittest.mock import patch
    import desktop
    import export_transfer
    import text_export
    folder=Path(folder);out=folder/'exports';out.mkdir()
    window.setProperty(text_export.DIRECTORY_PROPERTY,str(out))
    window.monitor.pause(window.sid)
    target=out/'Учебная сессия.txt';target.write_text('old report',encoding='utf-8')
    result=dict(version=desktop.server.VERSION,cwd=os.getcwd(),synthetic=True,checks={},screenshots=[])
    state={'stage':0};started=time.monotonic()
    timer=QTimer(window);timer.setInterval(50);window.feedback_timer=timer
    def export(deny=False):
        with patch.object(QFileDialog,'getSaveFileName',return_value=(target.name,'Текст')):
            if deny:
                # Start the worker with a failing writer, not a global patch
                # that could affect another thread or the preference file.
                original=export_transfer.atomic_write
                def denied(path,writer):
                    if Path(path)==target:raise OSError(errno.EROFS,'synthetic read-only directory')
                    return original(path,writer)
                state['deny_patch']=patch.object(export_transfer,'atomic_write',side_effect=denied);state['deny_patch'].start()
                state['warning_patch']=patch.object(desktop.QMessageBox,'warning');state['warning']=state['warning_patch'].start()
            window.export_session()
    def done(error=None):
        timer.stop()
        if 'deny_patch' in state:state.pop('deny_patch').stop();state.pop('warning_patch').stop()
        if error:result['error']=str(error)
        result['ok']=not error
        if report_path:Path(report_path).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        window.close()
    def tick():
        try:
            if time.monotonic()-started>20:raise TimeoutError('Feedback smoke exceeded 20 seconds')
            stage=state['stage']
            if stage==0:
                for key,value in {'ip':'192.0.2.10','gateway':'192.0.2.1','mask':'255.255.255.0','dns':'2001:db8::53','switch':'demo-switch','port':'Gi1/0/1','vlan':'100','mac':'02:00:00:00:00:10'}.items():window.network_fields[key].setText(value)
                window.note('Synthetic feedback regression')
                window.port_panel.manual();window.port_panel.connected=('demo-switch','Gi1/0/1',300)
                window.port_panel.failure((window.port_panel.connected,'Нет доступа к Graphene. Откройте порт в браузере и проверьте VPN / авторизацию.','access'))
                export(deny=True);state['stage']=1
            elif stage==1 and window.export_action.isEnabled():
                assert target.read_text(encoding='utf-8')=='old report'
                assert 'другую папку' in state['warning'].call_args.args[2]
                result['checks']['read_only_preserves_report_and_session']=bool(window.sid)
                state.pop('deny_patch').stop();state.pop('warning_patch').stop();export();state['stage']=2
            elif stage==2 and window.export_action.isEnabled():
                text=target.read_text(encoding='utf-8');assert window.sid in text and 'Synthetic feedback regression' in text
                result['checks']['relative_dialog_writes_user_folder']=True
                result['checks']['last_successful_folder_remembered']=window.property(text_export.DIRECTORY_PROPERTY)==str(out)
                class Clipboard:
                    text=''
                    def setText(self,text):self.text=text
                clipboard=Clipboard()
                with patch.object(desktop.QApplication,'clipboard',return_value=clipboard):window.network_fields['dns'].copy_value()
                assert clipboard.text=='2001:db8::53';result['checks']['full_field_copy']=True
                with patch.object(QFileDialog,'getSaveFileName',return_value=('Stream.txt','Текст')):window.stream_panel.save_report()
                state['stage']=3
            elif stage==3 and not window.stream_panel.report_pending:
                assert (out/'Stream.txt').is_file();result['checks']['stream_relative_dialog_export']=True
                # Keep this synthetic source error visible while exercising a
                # retry; no request to a real switch or Graphene is made.
                with patch.object(desktop.graphene_port,'client_session'),patch.object(desktop.PortPanel,'age'),patch('port_widgets.PortFetch.start'):
                    window.port_panel.refresh()
                assert 'Нет доступа' in window.port_panel.status.text();result['checks']['graphene_retry_keeps_error']=True
                window.resize(900,640);state['stage']=4
            elif stage in (4,5):
                size='900x640' if stage==4 else '1100x760'
                path=folder/('feedback-'+size+'.png');assert window.grab().save(str(path))
                result['screenshots'].append(str(path));result['checks'][size]=[window.width(),window.height()]
                if stage==4:window.resize(1100,760);state['stage']=5
                else:done()
        except Exception as error:done(error)
    timer.timeout.connect(tick);timer.start()
