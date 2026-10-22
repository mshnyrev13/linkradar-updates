"""Consistent streaming UTF-8 TXT/JSON export, independent of graph reduction."""
import io
import json
import time
from itertools import groupby
import server
from measurement_metrics import iter_jitter,database_cadence

HEADER=('LinkRadar — полная сессия\n'
        'RTT/jitter EWMA (alpha=1/16): миллисекунды; mean_delta: средняя абсолютная ΔRTT. ts: Unix seconds.\n'
        'Graphene RX/TX: бит/с; packets/errors/drops: пакетов/с. Пропуски не заполняются.\n\n')

def record(store,sid,kind,key,data,ts=None):
    if sid:
        store.execute('INSERT OR REPLACE INTO session_data VALUES (?,?,?,?,?)',
                      (sid,kind,str(key),time.time() if ts is None else ts,json.dumps(data,ensure_ascii=False)))

def record_many(store,rows,batch_size=512):
    """Serialize outside the writer mutex; bounded transactions leave probes room.

    Called by the UI I/O worker. A retry uses the same primary keys, including
    after a partial failure. The export barrier acknowledges the entire job.
    """
    for offset in range(0,len(rows),batch_size):
        batch=[(sid,kind,key,ts,json.dumps(value,ensure_ascii=False))
               for sid,kind,key,ts,value in rows[offset:offset+batch_size]]
        with store.lock:
            try:
                store.db.executemany('INSERT OR REPLACE INTO session_data VALUES (?,?,?,?,?)',batch)
                store.db.commit()
            except Exception:store.db.rollback();raise
    store.prune(force=True)

def rows(db,sql,args=()):
    cursor=db.execute(sql,args)
    while True:
        batch=cursor.fetchmany(512)
        if not batch:return
        for row in batch:yield dict(row)

def write_txt(store,sid,output,names=None,stop=None):
    """No full-history lists or giant JSON strings. Never writes a destination path."""
    def write(value):
        if stop and stop.is_set():raise InterruptedError('Экспорт отменён')
        output.write(value)
    def dump(value):write(json.dumps(value,ensure_ascii=False,allow_nan=False))
    def array(values):
        write('[');separator=''
        for value in values:write(separator);dump(value);separator=',\n'
        write(']')
    with store.reader() as db:
        session=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        if not session:raise ValueError('Сессия не найдена')
        session=dict(session);session['config']=json.loads(session['config']);config=session['config']
        cutoff=store.clock()-store.retention_seconds if store.retention_seconds else float('-inf')
        # Retention is enforced by the read predicate too; a paused writer need not prune first.
        query=' WHERE session=? AND ts>=?';args=(sid,cutoff)
        intervals={target['id']:(database_cadence(db,'samples','session=? AND target=?',(sid,target['id']),config['route_interval']) if target['mode']=='route' else config['interval']) for target in config['targets']}
        write(HEADER+'{\n"exported_at": ');dump(time.time());write(',\n"session": ');dump(session)
        write(',\n"statistics": {');separator=''
        for target in config['targets']:
            accumulator=server.Accumulator(intervals[target['id']])
            for point in rows(db,'SELECT * FROM samples'+query+' AND target=? ORDER BY ts',args+(target['id'],)):accumulator.add(point)
            stats=accumulator.stats(max(session['started'],cutoff),accumulator.last['ts'] if accumulator.last else session['started'])
            write(separator);dump(target['id']);write(': ');dump(dict(host=target['host'],**stats));separator=',\n'
        write('},\n"hop_statistics": {');separator=''
        for key,points in groupby(rows(db,'SELECT * FROM hop_samples'+query+' ORDER BY target,node,ts',args),lambda p:(p['target'],p['node'])):
            observed=database_cadence(db,'hop_samples','session=? AND target=? AND node=?',(sid,*key),config['route_interval'])
            accumulator=server.Accumulator(observed)
            for point in points:accumulator.add(point)
            write(separator);dump('/'.join(key));write(': ');dump(accumulator.stats(accumulator.first_ts,accumulator.last['ts']));separator=',\n'
        write('},\n"derived_samples": [');separator=''
        for target in config['targets']:
            points=rows(db,'SELECT * FROM samples'+query+' AND target=? ORDER BY ts',args+(target['id'],))
            for point in iter_jitter(points,intervals[target['id']]):write(separator);dump(point);separator=',\n'
        write('],\n"resolved_names": ');dump(names or {})
        for table in ('samples','hop_samples','routes','events','session_data'):
            write(',\n');dump(table);write(': ')
            def decoded(table=table):
                clause=' WHERE session=? AND (ts>=? OR kind=\'engine_state\')' if table=='session_data' else query
                for row in rows(db,'SELECT * FROM '+table+clause+' ORDER BY ts',args):
                    if table in ('routes','session_data'):row['data']=json.loads(row['data'])
                    yield row
            array(decoded())
        write('\n}\n')

def export_txt(store,sid,names=None):
    """Compatibility helper for callers explicitly requiring a string; UI streams."""
    output=io.StringIO();write_txt(store,sid,output,names);return output.getvalue()
