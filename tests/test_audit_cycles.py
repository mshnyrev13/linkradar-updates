"""Iterative QA: private profiles and deterministic synthetic source data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json
import sys
import tempfile
import threading
import time
import unittest
import random
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication
from qt_support import close_window,flush_io
import desktop,server,graphene_port,ui_tasks,stream_monitor,stream_widgets,session_export
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from PySide6.QtCore import QPersistentModelIndex
import traffic_widgets,traffic_capture
from log_widgets import suffix_overlap


@contextmanager
def window():
    with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
        widget=desktop.Window(Path(folder));widget.timer.stop()
        try:yield widget
        finally:
            close_window(widget);widget.store.db.close();widget.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


class AuditCycles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def test_graphene_ack_cache_does_not_reset_above_50000_points(self):
        with window() as w:
            w.sid='fixture';now=time.time();batches=[]
            def record(kind,key,data,rows=None,done=None):
                if rows:batches.append(len(rows))
                if done:done(True)
                return True
            data=dict(host='qa-switch',port='Gi1/0/1',series={k:[(now-6000+i,1) for i in range(6000)] for k in graphene_port.SENSORS})
            with patch.object(w,'record',side_effect=record):
                w.record_port(data)
                for rows in data['series'].values():rows.append((now+1,2))
                w.record_port(data);w.record_port(data)
            self.assertEqual(batches,[54000,9])

    def test_local_session_record_runs_off_the_gui_thread_and_export_barrier_waits(self):
        with window() as w:
            cfg=server.validate_config(dict(continuous=True,targets=[dict(host='192.0.2.1')]))
            w.sid='fixture';w.store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',(w.sid,time.time(),None,'running',json.dumps(cfg)))
            db=w.store.db;entered=threading.Event();release=threading.Event();threads=[]
            class SlowDB:
                def __getattr__(self,name):return getattr(db,name)
                def executemany(self,*args):
                    threads.append(threading.get_ident());entered.set();release.wait(.35)
                    return db.executemany(*args)
            original=w.store.db;w.store.db=SlowDB()
            try:
                started=time.monotonic();self.assertTrue(w.record('speed','fixture',dict(download=42)))
                elapsed=time.monotonic()-started
                self.assertTrue(entered.wait(1));release.set();flush_io(w)
                self.assertNotEqual(threads[0],threading.get_ident())
                self.assertLess(elapsed,.1)
                self.assertEqual(w.store.rows('SELECT count(*) AS n FROM session_data')[0]['n'],1)
            finally:release.set();w.store.db=original

    def test_close_does_not_wait_for_io_inside_qt_event(self):
        with window() as w:
            if not w.io:
                w.io=ui_tasks.IOQueue();w.io.completed.connect(w.io_completed);w.io.start()
            entered=threading.Event();release=threading.Event()
            w.io.submit(lambda:(entered.set(),release.wait(.4)))
            self.assertTrue(entered.wait(1))
            try:
                started=time.monotonic();w.close();elapsed=time.monotonic()-started
                self.assertLess(elapsed,.1)
                self.assertFalse(w.closed_clean)
            finally:release.set()

    def test_stream_keyboard_selection_updates_the_selected_chart(self):
        monitor=stream_monitor.StreamMonitor(clock=lambda:100)
        for i in range(3):
            key='obs:'+str(i);monitor.stream(key,100,label='Encoder '+str(i),state='live',bitrate=1000*(i+1))
            monitor.sample(key,100)
        panel=stream_widgets.StreamPanel(monitor=monitor);panel.resize(1000,700);panel.show();self.app.processEvents()
        try:
            panel.table.setCurrentCell(0,0);panel.table.setFocus();QTest.keyClick(panel.table,Qt.Key.Key_Down)
            self.app.processEvents();row=panel.table.currentRow();key=panel.table.item(row,0).data(Qt.ItemDataRole.UserRole)
            self.assertEqual(row,1);self.assertEqual(panel.selected,key)
            self.assertEqual(panel.plots[0].points[-1][1]['bitrate'],2000)
        finally:panel.shutdown();panel.close();panel.deleteLater()

    def test_rolling_stream_events_keep_selection_and_the_visible_message(self):
        monitor=stream_monitor.StreamMonitor(clock=lambda:1000)
        for i in range(5000):monitor.event('fixture','info','event '+str(i)+' · '+'текст '*18)
        panel=stream_widgets.StreamPanel(monitor=monitor);panel.resize(900,640);panel.show();self.app.processEvents()
        try:
            editor=panel.events;cursor=editor.document().find('event 200');editor.setTextCursor(cursor)
            editor.verticalScrollBar().setValue(500);self.app.processEvents()
            selected=editor.textCursor().selectedText();visible=editor.firstVisibleBlock().text()
            monitor.event('fixture','info','new event');panel.refresh();self.app.processEvents()
            self.assertEqual(editor.textCursor().selectedText(),selected)
            self.assertEqual(editor.firstVisibleBlock().text(),visible)
        finally:panel.shutdown();panel.close();panel.deleteLater()

    def test_packet_row_identity_survives_a_rolling_refresh(self):
        panel=traffic_widgets.TrafficPanel();panel.timer.stop();panel.engine.clock=lambda:1000
        try:
            for i in range(30):panel.engine.add(dict(raw=b'\0'*14,length=14,ts=i))
            panel.refresh();index=QPersistentModelIndex(panel.model.index(10,1));original=panel.model.rows[10].number
            panel.engine.frames.popleft();panel.engine.add(dict(raw=b'\0'*14,length=14,ts=31));panel.refresh()
            self.assertTrue(index.isValid());self.assertEqual(index.row(),9)
            self.assertEqual(panel.model.rows[index.row()].number,original)
        finally:panel.shutdown();panel.deleteLater()

    def test_packet_keyboard_selection_updates_its_details(self):
        panel=traffic_widgets.TrafficPanel();panel.timer.stop();panel.engine.clock=lambda:1000
        for i in range(20):panel.engine.add(dict(raw=b'\0'*14,length=14,ts=i))
        panel.resize(900,300);panel.show();panel.refresh();self.app.processEvents()
        try:
            panel.table.setCurrentIndex(panel.model.index(5,0));panel.inspect(panel.model.index(5,0))
            panel.table.setFocus();QTest.keyClick(panel.table,Qt.Key.Key_Down);self.app.processEvents()
            self.assertEqual(panel.selected,panel.model.rows[6].number)
        finally:panel.shutdown();panel.close();panel.deleteLater()

    def test_saturated_io_queue_explains_export_deferral_without_creating_a_file(self):
        with window() as w:
            w.sid='fixture';entered=threading.Event();release=threading.Event()
            w.io.submit(lambda:(entered.set(),release.wait(3)));self.assertTrue(entered.wait(1))
            for i in range(w.io.jobs.maxsize):w.io.submit(lambda:None)
            target=w.folder/'report.txt'
            try:
                with patch.object(desktop.QFileDialog,'getSaveFileName',return_value=(str(target),'txt')),patch.object(desktop.QMessageBox,'warning') as warning:
                    w.export_session();warning.assert_called_once()
                self.assertFalse(target.exists());self.assertTrue(w.export_action.isEnabled())
            finally:release.set()

    def test_route_keyboard_focus_selects_the_matching_hop(self):
        with window() as w:
            w.snapshot=dict(targets=[dict(hops=[dict(node='first'),dict(node='second')])])
            w.table.setRowCount(2);w.show();self.app.processEvents()
            with patch.object(w,'render'),patch.object(w,'refresh'):
                w.table.setCurrentCell(0,0);w.table.setFocus();QTest.keyClick(w.table,Qt.Key.Key_Down)
                self.assertEqual(w.selected_node,'second')

    def test_graphene_cache_and_incoming_rows_obey_session_retention(self):
        with window() as w:
            now=[100000.];w.store.clock=lambda:now[0];w.sid='fixture';batches=[]
            def record(kind,key,data,rows=None,done=None):
                if rows:batches.extend(rows)
                if done:done(True)
                return True
            data=dict(host='qa-switch',port='Gi1/0/1',series={k:[(now[0]-server.RETENTION_SECONDS-1,1),(now[0],2)] for k in graphene_port.SENSORS})
            with patch.object(w,'record',side_effect=record):
                w.record_port(data);self.assertEqual(len(batches),9);self.assertEqual(len(w.port_seen),9)
                now[0]+=server.RETENTION_SECONDS+61;w.record_port(data)
                self.assertEqual(len(batches),9);self.assertEqual(w.port_seen,{})

    def test_retry_after_partial_local_record_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            store=server.Store(Path(folder)/'db');original=store.db;calls=[0]
            class FailingDB:
                def __getattr__(self,name):return getattr(original,name)
                def executemany(self,*args):
                    calls[0]+=1
                    if calls[0]==2:raise OSError('synthetic write failure')
                    return original.executemany(*args)
            rows=[['s','graphene',str(i),100,dict(value=i)] for i in range(600)]
            try:
                store.db=FailingDB()
                with self.assertRaises(OSError):session_export.record_many(store,rows)
                store.db=original;session_export.record_many(store,rows)
                self.assertEqual(len(store.rows('SELECT * FROM session_data')),600)
            finally:store.db=original;original.close()

    def test_control_repeated_log_lines_match_a_bruteforce_overlap(self):
        rng=random.Random(47)
        for _ in range(2000):
            old=[rng.choice('abc') for _ in range(rng.randrange(30))]
            new=[rng.choice('abc') for _ in range(rng.randrange(30))]
            expected=max([0]+[n for n in range(1,min(len(old),len(new))+1) if old[-n:]==new[:n]])
            self.assertEqual(suffix_overlap(old,new),expected)

    def test_control_packet_model_random_append_expiry_and_new_capture(self):
        rng=random.Random(47);model=traffic_widgets.FramesModel();rows=[];number=0
        for iteration in range(400):
            previous=rows[len(rows)//2].number if rows else None
            tracked=QPersistentModelIndex(model.index(len(rows)//2,1)) if rows else None
            rows=rows[rng.randrange(min(5,len(rows))+1):]
            if iteration%31==0:rows=[];number+=100
            for _ in range(rng.randrange(1,6)):
                number+=1;rows.append(traffic_capture.Frame(number,number,1000,14,b'\0'*14,str(number),'fixture','UDP','fixture'))
            model.replace_rows(list(rows));self.assertEqual(model.rowCount(),len(rows))
            for row in (0,len(rows)//2,len(rows)-1):self.assertEqual(model.data(model.index(row,1)),str(rows[row].number))
            if tracked and previous in {frame.number for frame in rows}:
                self.assertTrue(tracked.isValid());self.assertEqual(model.data(tracked),str(previous))


if __name__=='__main__':unittest.main()
