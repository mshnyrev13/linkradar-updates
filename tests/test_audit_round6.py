"""Round 6: active-history cost and event-view regressions; no live network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,json,tempfile,time,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server,desktop
from qt_support import close_window
from PySide6.QtWidgets import QApplication,QPlainTextEdit
from PySide6.QtGui import QTextCursor
from plot_sampling import chart_samples
from log_widgets import update_lines

class RoundSix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def test_route_lookup_does_not_scan_another_targets_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store=server.Store(Path(folder)/'db',clock=lambda:1000)
            try:
                cfg=server.validate_config({'targets':[{'host':'192.0.2.1'}]});tid=cfg['targets'][0]['id']
                store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('new',900,1000,'completed',json.dumps(cfg)))
                store.db.executemany('INSERT INTO routes VALUES (?,?,?,?,?)',(('old','other',i/20,'complete','{"hops":[]}') for i in range(20000)));store.db.commit()
                class Counter:
                    calls=0
                    def is_set(self):self.calls+=1;return False
                counter=Counter();snapshot=server.Monitor(store).snapshot('new',300,compact=True,cancel=counter)
                self.assertIsNone(snapshot['targets'][0]['route'])
                self.assertLess(counter.calls,40,'Too much SQLite work for a target with no route')
            finally:store.db.close()

    def test_quiet_event_log_is_not_reformatted_on_each_probe(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
            w=desktop.Window(Path(folder),service_folder=False);w.timer.stop();w.address.setText('192.0.2.1');w.toggle()
            try:
                snap=w.monitor.snapshot(w.sid,300,compact=True);now=time.time()
                events=[dict(cursor=i,ts=now-i,message='synthetic '+str(i)) for i in range(500)]
                snap['events']=events;snap['events_cursor']=1000;snap['events_cutoff']=0;w.render(snap)
                cursor=w.logs.textCursor();cursor.setPosition(10);cursor.setPosition(30,QTextCursor.MoveMode.KeepAnchor);w.logs.setTextCursor(cursor)
                selected=cursor.selectedText();w.logs.verticalScrollBar().setValue(0)
                next_snap={**snap,'events':[]}
                with patch.object(desktop,'format_timestamp',wraps=desktop.format_timestamp) as formatter:
                    w.render(next_snap);self.assertEqual(formatter.call_count,0)
                self.assertEqual(w.logs.textCursor().selectedText(),selected);self.assertEqual(w.logs.verticalScrollBar().value(),0)
                self.assertEqual(len(w.snapshot['events']),500);self.assertIn('synthetic 499',w.logs.toPlainText())
                expired={**snap,'events':[],'events_cutoff':now-2}
                with patch.object(desktop,'format_timestamp',wraps=desktop.format_timestamp) as formatter:
                    w.render(expired);self.assertEqual(formatter.call_count,0)
                self.assertEqual(len(w.snapshot['events']),3);self.assertNotIn('synthetic 499',w.logs.toPlainText())
                self.assertEqual(len(w.event_labels),3)
                incoming={**snap,'events':[dict(cursor=1001,ts=now+1,message='new event')],'events_cursor':1001,'events_cutoff':now-2}
                with patch.object(desktop,'format_timestamp',wraps=desktop.format_timestamp) as formatter:
                    w.render(incoming);self.assertEqual(formatter.call_count,1)
                self.assertIn('new event',w.logs.toPlainText())
                # A caller may correct an event in place; identity alone is insufficient.
                w.snapshot['events'][0]['message']='corrected event'
                w.render(w.snapshot);self.assertIn('corrected event',w.logs.toPlainText())
                # A missing/previous session must clear the journal, not reuse it.
                w.snapshot['session']=None;w.render({**snap,'events':[]})
                self.assertEqual(w.logs.toPlainText(),'');self.assertFalse(w.event_labels)
            finally:close_window(w);w.store.db.close();w.deleteLater();self.app.processEvents()

    def test_old_hop_index_migrates_without_changing_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db';store=server.Store(path,recover=False)
            row=('s','t',100,'c',1,'node','192.0.2.1','ok',12.5,'synthetic')
            store.db.execute('INSERT INTO hop_samples VALUES (?,?,?,?,?,?,?,?,?,?)',row)
            store.db.execute('DROP INDEX hop_node_idx')
            store.db.execute('CREATE INDEX hop_node_idx ON hop_samples(session,target,node,ts)')
            store.db.commit();store.db.close()
            reopened=server.Store(path,recover=False)
            try:
                self.assertEqual(tuple(reopened.db.execute('SELECT * FROM hop_samples').fetchone()),row)
                self.assertEqual([r[2] for r in reopened.db.execute('PRAGMA index_info(hop_node_idx)')],['session','target','node','ts','hop','status','latency'])
                self.assertEqual(reopened.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            finally:reopened.db.close()

    def test_failed_index_upgrade_rolls_back_old_index_and_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            store=server.Store(Path(folder)/'db',recover=False)
            try:
                store.db.execute('DROP INDEX hop_node_idx');store.db.execute('CREATE INDEX hop_node_idx ON hop_samples(session,target,node,ts)');store.db.commit()
                def deny_create(action,*_):return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_INDEX else sqlite3.SQLITE_OK
                store.db.set_authorizer(deny_create)
                with self.assertRaises(sqlite3.DatabaseError):store._upgrade_hop_index()
                store.db.set_authorizer(None)
                self.assertEqual([r[2] for r in store.db.execute('PRAGMA index_info(hop_node_idx)')],['session','target','node','ts'])
                self.assertFalse(store.db.in_transaction)
            finally:store.db.set_authorizer(None);store.db.close()

    def test_short_and_wide_hop_aggregates_exclude_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            store=server.Store(Path(folder)/'db',recover=False,clock=lambda:1000)
            try:
                cfg=server.validate_config({'targets':[{'host':'192.0.2.1'}]});tid=cfg['targets'][0]['id']
                store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('s',0,1000,'completed',json.dumps(cfg)))
                statuses=[('ok',10),('timeout',None),('ok',30),('error',999)]
                rows=[('s',tid,start+i*10,str(i),1,'node','192.0.2.1',status,latency,'synthetic') for start in (100,900) for i,(status,latency) in enumerate(statuses)]
                store.db.executemany('INSERT INTO hop_samples VALUES (?,?,?,?,?,?,?,?,?,?)',rows);store.db.commit()
                monitor=server.Monitor(store);monitor.hostname=lambda ip:ip
                for period,multiplier in ((100,1),(1000,2)):
                    hop=monitor.snapshot('s',period,compact=True)['targets'][0]['hops'][0];stats=hop['stats']
                    self.assertEqual((stats['count'],stats['attempted'],stats['errors'],stats['ok']),(4*multiplier,3*multiplier,multiplier,2*multiplier))
                    self.assertEqual((stats['loss'],stats['avg'],stats['min'],stats['max']),(33.333,20,10,30))
            finally:store.db.close()

    def test_stream_flaps_have_a_pixel_budget_without_false_connections(self):
        for missing in (False,True):
            points=[(i,{'a':None if missing and i%2 else i%101,'b':i%13},'reconnecting' if i%2 else 'live') for i in range(43200)]
            sampled=chart_samples(points,0,43200,260,['a','b'])
            self.assertLess(len(sampled),260*20)
            self.assertEqual((sampled[0][0],sampled[-1][0]),(0,43199))
            self.assertEqual(max(p[1]['a'] for p in sampled if p[1]['a'] is not None),100)
            self.assertTrue(any(p[2]=='reconnecting' for p in sampled))
            for before,after in zip(sampled,sampled[1:]):
                if after[3]:continue
                if before[1]['a'] is not None and after[1]['a'] is not None:
                    self.assertTrue(all(p[1]['a'] is not None for p in points[before[0]:after[0]+1]))

    def test_bulk_log_expiry_preserves_surviving_selection_and_text(self):
        editor=QPlainTextEdit();old=['Synthetic event '+str(i) for i in range(50000)]
        try:
            update_lines(editor,[],old)
            cursor=QTextCursor(editor.document().findBlockByNumber(45000));cursor.select(QTextCursor.SelectionType.BlockUnderCursor);editor.setTextCursor(cursor)
            selected=editor.textCursor().selectedText();new=old[40000:]+['new event']
            update_lines(editor,old,new)
            self.assertEqual(editor.toPlainText(),'\n'.join(new))
            self.assertEqual(editor.textCursor().selectedText(),selected)
            self.assertEqual(editor.textCursor().blockNumber(),5000)
        finally:editor.deleteLater();self.app.processEvents()

    def test_multiline_error_expiry_removes_whole_events(self):
        editor=QPlainTextEdit();old=['Error 😀\nsecond line','CRLF\r\nsecond line','CR\rsecond line','Paragraph\u2029second line','surviving 😀 event','last']
        try:
            update_lines(editor,[],old)
            cursor=editor.document().find('surviving 😀 event');editor.setTextCursor(cursor)
            new=old[4:]+['new event'];update_lines(editor,old,new)
            self.assertEqual(editor.toPlainText(),'\n'.join(new))
            self.assertEqual(editor.textCursor().selectedText(),'surviving 😀 event')
        finally:editor.deleteLater();self.app.processEvents()

if __name__=='__main__':unittest.main()
