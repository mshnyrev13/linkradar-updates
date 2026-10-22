"""Rolling disk history and complete close cleanup, using synthetic local data."""
import sys,tempfile,json,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server,session_export

class RetentionTests(unittest.TestCase):
 def test_rolling_twelve_hours_across_all_data_and_pause_state(self):
  with tempfile.TemporaryDirectory() as folder:
   now=[100000.];s=server.Store(Path(folder)/'network.sqlite3',retention_seconds=server.RETENTION_SECONDS,clock=lambda:now[0])
   config=server.validate_config({'continuous':True,'targets':[{'host':'192.0.2.1'}]});target=config['targets'][0]['id']
   s.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('s',1,None,'running',json.dumps(config)))
   old=now[0]-server.RETENTION_SECONDS+10
   for ts in (old,now[0]):
    s.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('s',target,ts,'ok',10,'192.0.2.1',''))
    s.execute('INSERT INTO hop_samples VALUES (?,?,?,?,?,?,?,?,?,?)',('s',target,ts,'cycle',1,'node','192.0.2.1','ok',10,''))
    s.execute('INSERT INTO routes VALUES (?,?,?,?,?)',('s',target,ts,'complete',json.dumps({'hops':[]})))
    s.execute('INSERT INTO events VALUES (?,?,?,?,?)',('s',ts,target,'info','synthetic'))
    session_export.record(s,'s','graphene',str(ts),{'value':1},ts)
   session_export.record(s,'s','engine_state','pause',{'paused':True},1)
   now[0]+=20;s.prune()
   for table in ('samples','hop_samples','routes','events'):
    rows=s.rows('SELECT * FROM '+table);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['ts'],100000)
   self.assertEqual(len(s.rows("SELECT * FROM session_data WHERE kind='graphene'")),1)
   self.assertEqual(len(s.rows("SELECT * FROM session_data WHERE kind='engine_state'")),1)
   self.assertEqual(len(s.rows('SELECT * FROM sessions')),1)
   doc=json.loads(session_export.export_txt(s,'s').split('\n\n',1)[1]);self.assertEqual(len(doc['samples']),1);self.assertEqual(len(doc['hop_samples']),1)
   now[0]+=server.RETENTION_SECONDS+1;s.prune()
   self.assertEqual(s.rows('SELECT * FROM samples'),[]);self.assertEqual(s.rows('SELECT * FROM routes'),[])
   s.db.close()

 def test_clear_removes_all_rows_and_service_logs_preserves_exports_and_keys(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);s=server.Store(root/'network.sqlite3',retention_seconds=server.RETENTION_SECONDS)
   config=server.validate_config({'targets':[{'host':'192.0.2.1'}]})
   s.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('s',time.time(),None,'running',json.dumps(config)))
   target=config['targets'][0]['id'];now=time.time()
   s.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('s',target,now,'ok',10,'192.0.2.1',''))
   s.execute('INSERT INTO hop_samples VALUES (?,?,?,?,?,?,?,?,?,?)',('s',target,now,'cycle',1,'node','192.0.2.1','ok',10,''))
   s.execute('INSERT INTO routes VALUES (?,?,?,?,?)',('s',target,now,'complete',json.dumps({'hops':[]})))
   s.event('s',target,'info','synthetic')
   session_export.record(s,'s','speed','test',{'download':10})
   for name in ('service-out.log','service-error.log','saved-export.txt','service.key'):(root/name).write_text('synthetic',encoding='utf-8')
   monitor=server.Monitor(s);monitor.clear()
   for table in ('sessions','samples','hop_samples','routes','events','session_data'):self.assertEqual(s.rows('SELECT * FROM '+table),[])
   for name in ('service-out.log','service-error.log'):self.assertEqual((root/name).read_text(encoding='utf-8'),'')
   for name in ('saved-export.txt','service.key'):self.assertEqual((root/name).read_text(encoding='utf-8'),'synthetic')
   self.assertEqual(s.db.execute('PRAGMA freelist_count').fetchone()[0],0)
   wal=root/'network.sqlite3-wal';self.assertTrue(not wal.exists() or wal.stat().st_size==0)
   s.db.close()

if __name__=='__main__':unittest.main()
