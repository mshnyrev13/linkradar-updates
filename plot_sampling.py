"""Pixel-based rendering samples; raw histories and exports stay unchanged."""
from bisect import bisect_left
import math


def chart_samples(points, start, end, width, keys, gap_seconds=3):
    """Keep pixel extrema, state/presence representatives and real line breaks.

    Repeated flaps within one pixel cannot usefully be drawn individually.
    Keep both ends of each state/presence and the first/last boundary there;
    a hidden interruption breaks the next retained segment. Raw history and
    hover stay complete. Decimated spacing alone never invents a break.
    """
    if not points:
        return []
    width = max(1, int(width))
    duration = max(1, end - start)
    selected = {0, len(points) - 1}
    bucket = None
    first = last = 0
    extrema = {};states={};presence={};boundaries=[];counts=[];count=0
    previous = None

    def flush():
        selected.update((first, last))
        for low, high in extrema.values():
            selected.update((low[1], high[1]))
        for pair in list(states.values())+list(presence.values()):selected.update(pair)
        for index in boundaries[:1]+boundaries[-1:]:selected.update((index-1,index))

    for index, point in enumerate(points):
        ts, values, state = point
        pixel = int((ts - start) / duration * width)
        if bucket is not None and pixel != bucket:
            flush()
            extrema = {};states={};presence={};boundaries=[]
        if pixel != bucket:
            bucket = pixel
            first = index
        last = index
        boundary=previous is not None and (ts-previous[0]>gap_seconds or state!=previous[2])
        interrupted=previous is not None and ts-previous[0]>gap_seconds
        if state not in states:states[state]=[index,index]
        else:states[state][1]=index
        for key in keys:
            value = values.get(key)
            identity=(key,value is None)
            if identity not in presence:presence[identity]=[index,index]
            else:presence[identity][1]=index
            if previous is not None and (value is None)!=(previous[1].get(key) is None):boundary=True;interrupted=True
            if value is None:
                continue
            if key not in extrema:
                extrema[key] = ((value, index), (value, index))
            else:
                low, high = extrema[key]
                extrema[key] = (
                    (value, index) if value < low[0] else low,
                    (value, index) if value > high[0] else high,
                )
        if boundary:boundaries.append(index)
        if interrupted:count+=1
        counts.append(count)
        previous = point
    flush()
    result=[];before=0
    for index in sorted(selected):
        gap=bool(index and points[index][0]-points[index-1][0]>gap_seconds)
        hidden=index>before+1 and counts[index]>counts[before]
        result.append((*points[index],gap or hidden));before=index
    return result


def line_samples(points, start, end, width, gap_seconds):
    """Bound dense missing-data flaps per pixel, preserving extrema and breaks."""
    if not points:return []
    selected={0,len(points)-1};bucket=None;first=last=0;low=high=None
    presence={};boundaries=[];counts=[];count=0
    width=max(1,int(width));duration=max(1,end-start)
    def flush():
        selected.update((first,last))
        if low is not None:selected.update((low[1],high[1]))
        for pair in presence.values():selected.update(pair)
        for index in boundaries[:1]+boundaries[-1:]:selected.update((index-1,index))
    for index,(ts,value) in enumerate(points):
        pixel=int((ts-start)/duration*width)
        if bucket is not None and pixel!=bucket:flush();low=high=None;presence={};boundaries=[]
        if pixel!=bucket:bucket=pixel;first=index
        last=index
        missing=value is None
        if missing not in presence:presence[missing]=[index,index]
        else:presence[missing][1]=index
        if index:
            previous=points[index-1]
            if ts-previous[0]>gap_seconds or missing!=(previous[1] is None):boundaries.append(index);count+=1
        counts.append(count)
        if value is not None:
            if low is None:low=high=(value,index)
            else:
                if value<low[0]:low=(value,index)
                if value>high[0]:high=(value,index)
    flush()
    result=[];before=0
    for index in sorted(selected):
        gap=bool(index and points[index][0]-points[index-1][0]>gap_seconds)
        hidden=index>before+1 and counts[index]>counts[before]
        result.append((*points[index],gap or hidden));before=index
    return result


def nearest_point(points, when, key=lambda point:point[0]):
    """Exact nearest point in a sorted raw history, with stable earlier ties."""
    if not points:return None
    index=bisect_left(points,when,key=key)
    if index==0:return points[0]
    if index==len(points) or when-key(points[index-1])<=key(points[index])-when:
        index=bisect_left(points,key(points[index-1]),key=key)
    return points[index]


def axis_label(value, width, measure, digits=2):
    """Fit numeric ticks; exact values remain available in the hover tooltip."""
    if value is None or not math.isfinite(value):return '—'
    for scale,suffix in ((1,''),(1e3,'к'),(1e6,'М'),(1e9,'Г'),(1e12,'Т')):
        for precision in range(digits,-1,-1):
            text=f'{value/scale:.{precision}f}'
            if precision:text=text.rstrip('0').rstrip('.')
            text=text.replace('.',',')+suffix
            if measure(text)<=width:return text
    return f'{value:.0e}'


def scale_headroom(peak):
    top=peak*1.15
    return top if math.isfinite(top) else peak


def nice_scale(peak):
    """Round a positive scale without overflowing on a finite source value."""
    target=scale_headroom(peak)
    step=10.0**math.floor(math.log10(target)) if target>0 else 1
    top=math.ceil(target/step)*step
    return top if math.isfinite(top) else target


def bucket_points(rows):
    """Keep extrema/status representatives with bounded cost even during flapping.

    First/last of each status and first/last continuity boundary are retained.
    A hidden interruption breaks the next retained segment; it cannot become a
    false continuous line. Aggregate counters still use every original probe.
    """
    if len(rows)<2:return list(rows)
    extrema={};states={};boundaries=[];counts=[];count=0
    for index,point in enumerate(rows):
        status=point['status']
        if status not in states:states[status]=[index,index]
        else:states[status][1]=index
        for metric in ('latency','jitter'):
            value=point.get(metric)
            if value is None or point['status']!='ok':continue
            if metric not in extrema:extrema[metric]=((value,index),(value,index))
            else:
                low,high=extrema[metric]
                extrema[metric]=((value,index) if value<low[0] else low,
                                 (value,index) if value>high[0] else high)
        if index:
            before=rows[index-1]
            if (point['break_before'] or point['status']!=before['status']
                    or (point.get('jitter') is None)!=(before.get('jitter') is None)):
                boundaries.append(index);count+=1
        counts.append(count)
    # Collapse repeated transitions within a bucket, retaining both ends.
    selected={0,len(rows)-1}
    for first,last in states.values():selected.update((first,last))
    for index in boundaries[:1]+boundaries[-1:]:selected.update((index-1,index))
    for low,high in extrema.values():selected.update((low[1],high[1]))
    result=[];previous=0
    for index in sorted(selected):
        result.append(dict(rows[index],break_before=rows[index]['break_before'] or counts[index]>counts[previous]))
        previous=index
    return result


def probe_samples(points, interval):
    """Same visible extrema contract for the legacy and native read models."""
    import interface_probes
    step=max(1,math.ceil(len(points)/900));graph=[];bucket=[];previous=None
    for point in points:
        point=dict(point)
        point['break_before']=bool(previous and (point['ts']-previous['ts']>interval*3+2
            or point.get('ip')!=previous.get('ip')
            or not interface_probes.same_path(point,previous)))
        bucket.append(point);previous=point
        if len(bucket)==step:graph.extend(bucket_points(bucket));bucket=[]
    if bucket:graph.extend(bucket_points(bucket))
    return graph,step
