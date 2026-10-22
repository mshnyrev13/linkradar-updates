"""Native UI read model. WAL reads never hold the measurement writer mutex.

Only the visible time series is materialized, in at most 900 buckets. Each keeps
endpoints, RTT/jitter extrema and real continuity boundaries. Hop table
aggregates run in SQLite; unselected hops do not allocate histories in Python.
"""
import json
import math
import time
from measurement_metrics import Jitter,database_cadence
import interface_probes
from plot_sampling import bucket_points


def series(db, table, where, args, begin, interval, accumulator):
    prefix = list(db.execute('SELECT ts,status,latency,ip,detail FROM '+table+
        ' WHERE '+where+' AND ts<? ORDER BY ts DESC LIMIT 256', (*args, begin)))
    state = Jitter(interval)
    for row in reversed(prefix):state.add(dict(row))
    total = db.execute('SELECT count(*) FROM '+table+' WHERE '+where+' AND ts>=?', (*args, begin)).fetchone()[0]
    step=max(1,math.ceil(total/900));graph=[];bucket=[];last=None
    for i,row in enumerate(db.execute('SELECT * FROM '+table+' WHERE '+where+' AND ts>=? ORDER BY ts', (*args,begin))):
        point=dict(row);point['jitter'],point['delta']=state.add(point)
        point['break_before']=bool(last and (point['ts']-last['ts']>interval*3+2 or point.get('ip')!=last.get('ip') or not interface_probes.same_path(point,last)))
        bucket.append(point)
        accumulator.add(point,derived=True);last=point
        if (i+1)%step==0 or i+1==total:
            graph.extend(bucket_points(bucket));bucket=[]
    stats=accumulator.stats(begin,last['ts'] if last else begin)
    if last:stats['jitter']=last['jitter']
    return graph,last,stats,step


def snapshot(monitor,sid,window,focus_end=None,event_after=0,selected_node=None,cancel=None):
    # Import here: server owns the measurement model and imports us only on request.
    import server
    with monitor.store.reader() as db:
        if cancel:db.set_progress_handler(lambda:int(cancel.is_set()),1000)
        row=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        if not row:return dict(session=None,targets=[],events=[],active_id=monitor.sid)
        session=dict(row);config=json.loads(session['config']);session['config']=config
        end=session['ended'] or (monitor.pause_at if monitor.sid==sid and monitor.paused and monitor.pause_at else time.time())
        analysis_end=min(end,max(session['started'],focus_end)) if focus_end is not None else end
        cutoff=monitor.store.clock()-monitor.store.retention_seconds if monitor.store.retention_seconds else float('-inf')
        begin=max(session['started'],analysis_end-window,cutoff)
        session.update(elapsed=max(0,end-session['started']),remaining=None if config.get('continuous') else max(0,config['duration_minutes']*60-(end-session['started'])),analysis_start=begin,analysis_end=analysis_end)
        targets=[]
        for target in config['targets']:
            tid=target['id'];interval=config.get('route_interval',2.5) if target['mode']=='route' else config['interval']
            where='session=? AND target=? AND ts<=?';args=(sid,tid,analysis_end)
            if target['mode']=='route':interval=database_cadence(db,'samples',where,args,interval)
            graph,last,stats,step=series(db,'samples',where,args,begin,interval,server.Accumulator(interval))
            latest=db.execute('SELECT * FROM samples WHERE session=? AND target=? AND ts<=? ORDER BY ts DESC LIMIT 1',(sid,tid,end)).fetchone()
            route=db.execute('SELECT * FROM routes WHERE '+where+' ORDER BY ts DESC LIMIT 1',args).fetchone()
            route=dict(route) if route else None
            if route:route['data']=json.loads(route['data'])
            if focus_end is None and target['mode']=='icmp' and route and latest and latest['ip'] and route['data'].get('destination') and not interface_probes.same_ip(latest['ip'],route['data']['destination']):route=None
            route_epoch=monitor.route_epoch.get((sid,tid),0) if focus_end is None else 0
            # A time-range scan wins for short windows. Reading most of a
            # retained session is cheaper in node order from the covering index
            # (no table reads or temporary GROUP BY tree).
            retained_span=max(0,end-max(session['started'],cutoff))
            group_index='hop_node_idx' if analysis_end-begin>=retained_span*.5 else 'hop_idx'
            groups=db.execute('''SELECT node,hop,count(*) AS count,sum(status!='error') AS attempted,
                sum(status='error') AS errors,sum(status='ok') AS ok,
                avg(CASE WHEN status='ok' THEN latency END) AS avg,
                min(CASE WHEN status='ok' THEN latency END) AS min,
                max(CASE WHEN status='ok' THEN latency END) AS max,max(ts) AS latest,min(ts) AS first
                FROM hop_samples INDEXED BY '''+group_index+''' WHERE session=? AND target=? AND ts>=? AND ts<=? GROUP BY node''',(sid,tid,begin,analysis_end)).fetchall()
            nodes={}
            for group in groups:
                if group['latest']<max(config.get('interface_changed_at',0),route_epoch):continue
                old=nodes.get(group['hop'])
                if old is None or group['latest']>old['latest']:nodes[group['hop']]=group
            hops=[]
            for number,g in sorted(nodes.items()):
                if route and route['state']=='complete' and number>max((h['hop'] for h in route['data']['hops']),default=0):continue
                hw='session=? AND target=? AND node=? AND ts<=?';ha=(sid,tid,g['node'],analysis_end)
                hlast=dict(db.execute('SELECT * FROM hop_samples WHERE '+hw+' ORDER BY ts DESC LIMIT 1',ha).fetchone())
                answered=db.execute('SELECT ip FROM hop_samples WHERE '+hw+' AND ts>=? AND ip IS NOT NULL ORDER BY ts DESC LIMIT 1',(*ha,begin)).fetchone()
                ip=answered[0] if answered else None
                hs={k:g[k] for k in ('count','attempted','errors','ok')};hs['failed']=hs['attempted']-hs['ok'];hs['loss']=round(100*hs['failed']/hs['attempted'],3) if hs['attempted'] else None
                hs.update({k:round(g[k],2) if g[k] is not None else None for k in ('avg','min','max')})
                hg=[];hstep=1
                if selected_node==g['node']:
                    observed=database_cadence(db,'hop_samples',hw,ha,config['route_interval'])
                    hg,hlast,hs,hstep=series(db,'hop_samples',hw,ha,begin,observed,server.Accumulator(observed))
                hops.append(dict(hop=number,node=g['node'],ip=ip,last=hlast,name=monitor.hostname(ip),stats=hs,graph=hg,graph_step=hstep,interval=round((g['latest']-g['first'])/(g['count']-1),3) if g['count']>1 else config['route_interval'],destination=bool(route and interface_probes.same_ip(ip,route['data'].get('destination'))),source='route'))
            targets.append(dict(target,stats=stats,window_stats=stats,window_last=last,last=dict(latest) if latest else None,graph=graph,graph_step=step,route=route,hops=hops,interval=interval,current_route=None,tracing=(sid,tid) in monitor.route_jobs))
        events=[dict(r) for r in db.execute('SELECT rowid AS cursor,* FROM events WHERE session=? AND rowid>? AND ts>=? ORDER BY rowid LIMIT 2000',(sid,event_after,cutoff))]
        cursor=events[-1]['cursor'] if events else event_after
        return dict(session=session,targets=targets,events=list(reversed(events)),events_cursor=cursor,events_cutoff=cutoff,compact=True,selected_node=selected_node,active_id=monitor.sid,paused=monitor.paused if monitor.sid==sid else False,version=server.VERSION)
