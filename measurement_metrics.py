"""One ICMP jitter contract for charts, statistics and exports; no I/O."""
import math
import statistics
import interface_probes

def cadence(timestamps,fallback):
    """Robust recent observation cadence, independent of a chart viewport."""
    values=list(timestamps)[-257:]
    gaps=[b-a for a,b in zip(values,values[1:]) if b>a]
    return statistics.median(gaps) if gaps else fallback

def database_cadence(db,table,where,args,fallback):
    # Only internal table/predicate literals are accepted by callers.
    points=db.execute('SELECT ts FROM '+table+' WHERE '+where+' ORDER BY ts DESC LIMIT 257',args).fetchall()
    return cadence([row[0] for row in reversed(points)],fallback)

class Jitter:
    def __init__(self, interval=1):
        self.interval=interval;self.previous=None;self.value=0.
    def add(self,sample):
        before=self.previous
        good=sample['status']=='ok' and sample.get('latency') is not None
        continuous=(good and before is not None and 0<=sample['ts']-before['ts']<=self.interval*3+2
                    and sample.get('ip')==before.get('ip')
                    and interface_probes.same_path(sample,before)
                    and not sample.get('break_before'))
        delta=abs(sample['latency']-before['latency']) if continuous else None
        self.value=self.value+(delta-self.value)/16 if continuous else 0.
        self.previous=sample if good else None
        return (round(self.value,4) if continuous else None),delta

def iter_jitter(samples,interval=1):
    state=Jitter(interval)
    for sample in samples:
        value,delta=state.add(sample)
        yield dict(sample,jitter=value,delta=None if delta is None else round(delta,4))

def smooth_jitter(samples,interval):
    return list(iter_jitter(samples,interval))

def metrics(samples,interval=1):
    samples=list(samples)
    attempted=[s for s in samples if s['status']!='error']
    good=[s['latency'] for s in attempted if s['status']=='ok' and s.get('latency') is not None]
    points=smooth_jitter(samples,interval)
    deltas=[p['delta'] for p in points if p['delta'] is not None]
    streak=max_streak=0
    for s in samples:
        streak=streak+1 if s['status'] in ('timeout','refused','unreachable') else 0
        max_streak=max(streak,max_streak)
    ordered=sorted(good)
    latest=(samples[-1].get('jitter') if 'jitter' in samples[-1] else points[-1]['jitter']) if samples else None
    return dict(count=len(samples),attempted=len(attempted),ok=len(good),failed=len(attempted)-len(good),
        errors=len(samples)-len(attempted),loss=round(100*(len(attempted)-len(good))/len(attempted),3) if attempted else None,
        avg=round(statistics.mean(good),2) if good else None,min=round(min(good),2) if good else None,max=round(max(good),2) if good else None,
        p95=round(ordered[math.ceil(len(ordered)*.95)-1],2) if ordered else None,jitter=latest,
        mean_delta=round(statistics.mean(deltas),2) if deltas else None,max_streak=max_streak,
        max_gap=round(max((b['ts']-a['ts'] for a,b in zip(samples,samples[1:])),default=0),2))
