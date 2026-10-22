"""Read-only Graphene network series. No SNMP, browser cookies, or external writes."""
import json
import math
import re
import statistics
import time
import requests
import certifi
import ssl
import truststore
import bound_http

BASE='https://graphene.yandex-team.ru'
SENSORS=('rx','tx','rx_packets','tx_packets','rx_errs','tx_errs','rx_drop','tx_drop','flap')
PERIODS=((30,'30 сек'),(60,'1 мин'),(300,'5 мин'),(1800,'30 мин'),(3600,'1 час'),(10800,'3 часа'),(21600,'6 часов'),(43200,'12 часов'),(86400,'24 часа'))

class SourceError(Exception):pass

def read_json(response,limit=20*1024*1024,seconds=20):
    """Bound decompressed bytes during the transfer, including catalog replies."""
    try:
        payload=bytearray()
        with bound_http.Deadline(seconds,response):
            for chunk in response.iter_content(64*1024):
                if len(payload)+len(chunk)>limit:raise SourceError('Ответ Graphene слишком большой')
                payload.extend(chunk)
        try:return json.loads(payload)
        except (ValueError,RecursionError):raise SourceError('Graphene вернул некорректный JSON') from None
    finally:response.close()

def validate_target(host,port):
    host=host.strip();port=port.strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,252}',host):
        raise SourceError('Укажите точное имя коммутатора из Graphene')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./: -]{0,127}',port):
        raise SourceError('Укажите точное имя порта из Graphene')
    return host,port

def decode(data,begin,end):
    series={key:[] for key in SENSORS}
    if not isinstance(data,dict) or not isinstance(data.get('results'),list):raise SourceError('Неожиданный формат ответа Graphene')
    for result in data['results']:
        if not isinstance(result,dict):raise SourceError('Неожиданный формат результата Graphene')
        if result.get('error'):raise SourceError('Graphene не смог выполнить запрос')
        rows=result.get('series',[])
        if not isinstance(rows,list):raise SourceError('Неожиданный формат рядов Graphene')
        for row in rows:
            if not isinstance(row,dict):raise SourceError('Неожиданный формат ряда Graphene')
            columns=row.get('columns',[])
            if not isinstance(columns,list) or not all(isinstance(c,str) for c in columns):raise SourceError('Неожиданный формат столбцов Graphene')
            if 'time' not in columns:continue
            ti=columns.index('time')
            values_rows=row.get('values',[])
            if not isinstance(values_rows,list):raise SourceError('Неожиданный формат точек Graphene')
            for ci,key in enumerate(columns):
                if key not in series:continue
                for values in values_rows:
                    if not isinstance(values,list):raise SourceError('Неожиданный формат точки Graphene')
                    if len(values)<=max(ti,ci):continue
                    ts=values[ti]
                    if not finite_number(ts):continue
                    ts=ts/1000
                    if not begin<=ts<=end:continue
                    v=values[ci]
                    # Null is a gap; never convert it to an error-free zero.
                    if v is not None and (not finite_number(v) or v<0):v=None
                    series[key].append((ts,v))
    for key,points in series.items():
        # Multiple matching series would silently double-count a port. Reject ambiguity.
        seen={}
        for ts,value in points:
            if ts in seen and seen[ts]!=value:raise SourceError('Неоднозначные ряды порта в Graphene')
            seen[ts]=value
        series[key]=sorted(seen.items())
    return series

def finite_number(value):
    try:return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)
    except OverflowError:return False

def info(series):
    valid=[ts for points in series.values() for ts,v in points if v is not None]
    steps=[b[0]-a[0] for points in series.values() for a,b in zip(points,points[1:]) if b[0]>a[0]]
    return {'latest':max(valid) if valid else None,'step':statistics.median(steps) if steps else None}

def client_session():
    context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.load_verify_locations(certifi.where())
    client=requests.Session();client.mount('https://',bound_http.adapter(None,ssl_context=context));return client

def port_identity(value):
    value=value.lower().replace(' ','')
    names={'gi':'gigabitethernet','fa':'fastethernet','te':'tengigabitethernet','eth':'ethernet','po':'port-channel'}
    m=re.fullmatch(r'([a-z-]+)([0-9][0-9/:.]*)',value)
    return (names.get(m[1],m[1])+m[2]) if m else value

def resource_query(host,port,canonical=False):
    """Format a pasteable resource expression; no network request on Copy."""
    host,port=validate_target(host,port)
    if not canonical:
        if host.lower().endswith(('.yndx.net','.yandex.net','.yandex-team.ru')):host=host.split('.')[0]
        match=re.fullmatch(r'(Gi|Fa|Te|Eth|Po)([0-9][0-9/:.]*)',port,re.I)
        if match:
            names={'gi':'GigabitEthernet','fa':'FastEthernet','te':'TenGigabitEthernet','eth':'Ethernet','po':'Port-channel'}
            port=names[match[1].lower()]+match[2]
    iface=json.dumps(port,ensure_ascii=False) if ' ' in port else port
    return f'graph=[bytes packets errors] host={host} iface={iface}'

def resolve(host,port,session=None):
    host,port=validate_target(host,port);own=session is None;client=client_session() if own else session
    def suggest(fields,key):
        try:
            r=client.post(BASE+'/graphene/back/suggest',json={'fields':fields,'suggest':key},timeout=(5,15),verify=True,allow_redirects=False,stream=True)
            if r.status_code!=200:
                r.close();raise SourceError('Нет доступа к каталогу портов Graphene (HTTP '+str(r.status_code)+')')
            data=read_json(r,2*1024*1024)
            if not data.get('ok'):raise SourceError('Каталог Graphene не ответил')
            values=data.get('result',{}).get('suggest')
            if not isinstance(values,list):raise SourceError('Неожиданный формат каталога Graphene')
            return [v for v in values if isinstance(v,str)]
        except (requests.RequestException,TimeoutError):raise SourceError('Нет связи с каталогом Graphene') from None
        except ValueError:raise SourceError('Graphene требует доступ / авторизацию') from None
    try:
        candidates=[host]
        if host.lower().endswith(('.yndx.net','.yandex.net','.yandex-team.ru')):candidates.append(host.split('.')[0])
        canonical=None
        for name in candidates:
            matches=[v for v in suggest({'graph':'bytes','host':name},'host') if v.lower()==name.lower()]
            if len(matches)>1:raise SourceError('Неоднозначное имя коммутатора в Graphene')
            if matches:canonical=matches[0];break
        if not canonical:raise SourceError('Коммутатор не найден в Graphene: проверьте имя')
        ports=suggest({'graph':'bytes','host':canonical},'iface')
        matches=[v for v in ports if v.lower()==port.lower()]
        if not matches:matches=[v for v in ports if port_identity(v)==port_identity(port)]
        if len(matches)!=1:raise SourceError('Порт не найден в Graphene' if not matches else 'Неоднозначное имя порта в Graphene')
        return canonical,matches[0]
    finally:
        if own:client.close()

def fetch(host,port,period,end=None,session=None,previous=None):
    host,port=validate_target(host,port)
    if period not in dict(PERIODS):raise SourceError('Неизвестный период порта')
    end=end or time.time();begin=end-max(period,1800)
    reuse=(previous and (previous['host'],previous['port'],previous['period'])==(host,port,period)
           and previous.get('latest') is not None and 0<=end-previous.get('observed_end',0)<=period)
    if reuse:begin=max(begin,previous['latest']-max(120,(previous.get('step') or 30)*3))
    query={'filter':[{'host':[host],'iface':[port],'sensor':list(SENSORS)}],
           'meta':{},'project':'noc','table':'network',
           'time_interval':f'time >= {int(begin*1000)}ms and time <= {int(end*1000)}ms'}
    own=session is None;client=client_session() if own else session
    try:
        response=client.get(BASE+'/graphene/back/query',params={'db':'graphene','q':json.dumps(query)},timeout=(5,15),verify=True,allow_redirects=False,stream=True)
        if response.status_code in (401,403) or 300<=response.status_code<400:
            response.close();raise SourceError('Graphene требует доступ из корпоративной сети / авторизацию')
        if response.status_code!=200:
            response.close();raise SourceError('Graphene недоступен (HTTP '+str(response.status_code)+')')
        data=read_json(response)
    except (requests.RequestException,TimeoutError):raise SourceError('Нет связи с Graphene: проверьте корпоративную сеть / VPN') from None
    except ValueError:raise SourceError('Graphene вернул страницу вместо данных') from None
    finally:
        if own:client.close()
    series=decode(data,begin,end)
    if reuse:
        series={key:sorted({**dict(previous['series'][key]),**dict(points)}.items()) for key,points in series.items()}
    metadata=info(series);observed_end=end
    # Graphene publishes with a delay. Keep short windows on the last real
    # sample, with its original timestamp, rather than an empty wall-clock window.
    if metadata['latest'] is not None:end=metadata['latest']
    begin=end-period
    series={key:[(ts,v) for ts,v in points if begin<=ts<=end] for key,points in series.items()}
    return {'host':host,'port':port,'period':period,'begin':begin,'end':end,'observed_end':observed_end,'series':series,**metadata}

def url(host,port,period):
    from urllib.parse import urlencode
    host,port=validate_target(host,port)
    return BASE+'/dashboard/script/graphene.js?'+urlencode({'orgId':1,'from':f'now-{period}s','to':'now','query':f'graph=[bytes,packets,errors] host={host} iface="{port}"'})
