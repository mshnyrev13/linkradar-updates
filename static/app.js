'use strict';
(function(root){
function endpoint(value,port=1935){
  value=String(value).trim();
  if(/^rtmps?:\/\//i.test(value)){
    const u=new URL(value);if(u.username||u.password)throw new Error('Укажите адрес без логина и пароля');
    return {host:u.hostname.replace(/^\[|\]$/g,''),port:Number(u.port||(u.protocol==='rtmps:'?443:1935)),tls:u.protocol==='rtmps:'};
  }
  if(!value||/[/?#\s]/.test(value))throw new Error('Введите IP, имя или адрес RTMP-сервера');
  return {host:value,port:Number(port),tls:false};
}
function analyze(snapshot,profile){
  const result={status:'pending',reasons:[],required:(profile.bitrate+profile.audio/1000)*1.5,recommended:(profile.bitrate+profile.audio/1000)*2};
  const targets=snapshot?.targets||[],host=targets.find(t=>t.mode==='icmp')?.host||targets[0]?.host;
  const ping=targets.find(t=>t.mode==='icmp'&&t.host===host),tcp=targets.find(t=>t.mode==='tcp'&&t.host===host&&t.port===profile.port);
  const s=ping?.window_stats;result.ping=s;result.tcp=tcp?.window_stats;
  result.currentJitter=ping?.graph?.filter(q=>q.jitter!=null).at(-1)?.jitter;
  if(s?.failed>0)result.reasons.push(`${s.failed} ICMP-проб без ответа: возможны задержки или фильтрация ICMP`);
  if(s?.errors>0)result.reasons.push('Ошибки сетевых проб: оценка неполна');
  if(tcp?.window_stats?.failed>0)result.reasons.push('Неуспешные подключения к порту приёма');
  if(tcp?.window_stats?.errors>0)result.reasons.push('Ошибки проверки TCP-порта');
  if(s?.max>=500)result.reasons.push('Пики RTT ≥500 мс: возможны заметные задержки доставки');
  if(profile.upload!=null&&profile.upload<result.required)result.reasons.push('Указанная скорость отдачи ниже запаса 1,5× для видео и аудио');
  if(profile.dropped>0)result.reasons.push('В программе вещания зафиксированы сетевые dropped frames');
  if(profile.reconnects>0)result.reasons.push('Зафиксированы переподключения трансляции');
  const sufficient=s?.count>=20&&tcp?.window_stats?.count>=20&&s.errors===0&&tcp.window_stats.errors===0;
  result.status=result.reasons.length?'risk':sufficient?'observed':'pending';
  result.level=profile.dropped>0||profile.reconnects>0||(profile.upload!=null&&profile.upload<profile.bitrate+profile.audio/1000)?'bad':result.reasons.length?'warn':sufficient?'good':'unknown';
  result.missing=[];
  if(!s||s.count<20)result.missing.push('Дождитесь 20 ICMP-проб');
  if(!tcp)result.missing.push('Укажите сервер приёма и запустите новую проверку — TCP-порт ещё не проверен');
  else if(tcp.window_stats?.count<20)result.missing.push('Дождитесь 20 TCP-проверок');
  if(profile.upload==null)result.missing.push('Укажите измеренную отдачу до сервера в настройках RTMP');
  if(profile.dropped==null||profile.reconnects==null)result.missing.push('Проверьте сетевые пропуски и переподключения в OBS/vMix');
  return result;
}
root.Streaming={endpoint,analyze};if(typeof module!=='undefined')module.exports=root.Streaming;
})(globalThis);

'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="local-token"]').content;
const state = {snapshot:null, session:null, target:null, window:300, profile:'broadcast', connected:true, hop:null, focus:null};
const fmt = (v,d=1) => v == null ? '—' : Number(v).toLocaleString('ru-RU',{maximumFractionDigits:d});
const escapeHTML = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = ts => new Date(ts*1000).toLocaleString('ru-RU');
const clock = ts => new Date(ts*1000).toLocaleTimeString('ru-RU');
const duration = seconds => {seconds=Math.max(0,Math.floor(seconds)); return `${Math.floor(seconds/3600).toString().padStart(2,'0')}:${Math.floor(seconds/60)%60<10?'0':''}${Math.floor(seconds/60)%60}:${(seconds%60).toString().padStart(2,'0')}`;};
const labels = {running:'Идёт сбор',completed:'Период завершён',stopped:'Остановлена',interrupted:'Прервана',stable:'В порогах',risk:'Есть риск',pending:'Недостаточно данных',ok:'Ответ получен',timeout:'Нет ответа',refused:'Порт закрыт',error:'Ошибка проверки'};
let toastTimer;
function toast(message){$('toast').textContent=message; $('toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),6000);}
async function api(path,body){
  const response=await fetch(path,{method:body?'POST':'GET',headers:{'X-Local-Token':token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'Не удалось выполнить запрос');return data;
}
function selected(){return state.snapshot?.targets.find(t=>t.id===state.target)||state.snapshot?.targets[0];}
function addTarget(t={}){
  if($('target-inputs').children.length>=12){toast('Максимум 12 целей');return;}
  const row=document.createElement('div');row.className='target-input';
  row.innerHTML=`<input class="target-label" aria-label="Название цели" placeholder="Название" maxlength="80" value="${escapeHTML(t.label||'')}"><input class="target-host" aria-label="IP или имя хоста" placeholder="192.168.1.1 / server.example" required value="${escapeHTML(t.host||'')}"><select class="target-mode" aria-label="Протокол"><option value="icmp">ICMP</option><option value="tcp">TCP</option><option value="route">Маршрут</option></select><select class="target-family" aria-label="Семейство адреса"><option value="ipv4">IPv4</option><option value="ipv6">IPv6</option><option value="auto">Авто</option></select><input class="target-port" type="number" aria-label="TCP-порт" min="1" max="65535" value="${t.port||443}"><label class="target-required"><input type="checkbox" ${t.required===false?'':'checked'}>Обяз.</label><button type="button" class="remove-target" aria-label="Удалить цель">×</button>`;
  const addressRow=document.createElement('div');addressRow.className='address-choice';addressRow.innerHTML=`<button type="button" class="quiet resolve-address">IP из DNS</button><select class="target-address" aria-label="Фиксированный IP"><option value="">Системный DNS · автоматический выбор</option></select><span class="address-hint"></span>`;row.append(addressRow);
  row.querySelector('.resolve-address').onclick=async()=>{try{const r=await api('/api/resolve?'+new URLSearchParams({host:row.querySelector('.target-host').value}));const family=row.querySelector('.target-family').value;const addresses=r.addresses.filter(ip=>family==='auto'||ip.includes(':')===(family==='ipv6'));row.querySelector('.target-address').innerHTML='<option value="">Системный DNS · автоматический выбор</option>'+addresses.map(ip=>`<option value="${escapeHTML(ip)}">${escapeHTML(ip)}</option>`).join('');row.querySelector('.address-hint').textContent=addresses.length?'Можно зафиксировать адрес':'Нет адресов выбранного семейства';}catch(e){row.querySelector('.address-hint').textContent=e.message;}};
  row.querySelector('.target-host').oninput=row.querySelector('.target-family').onchange=()=>{row.querySelector('.target-address').innerHTML='<option value="">Системный DNS · автоматический выбор</option>';row.querySelector('.address-hint').textContent='';};
  row.querySelector('.target-mode').value=t.mode||'icmp';row.querySelector('.target-family').value=t.family||'ipv4';
  const update=()=>{const tcp=row.querySelector('.target-mode').value==='tcp';row.querySelector('.target-port').disabled=!tcp;row.querySelector('.target-port').title=tcp?'Порт сервиса':'Для ICMP порт не используется';};
  update();row.querySelector('.target-mode').addEventListener('change',update);row.querySelector('.remove-target').addEventListener('click',()=>row.remove());$('target-inputs').append(row);
}
function profile(name,replace=false){
  state.profile=name;document.querySelectorAll('.profile').forEach(b=>b.classList.toggle('active',b.dataset.profile===name));
  const descriptions={broadcast:'Проверьте шлюз и фактический адрес приёма потока. Порт зависит от протокола вещания.',contest:'Проверьте шлюз, сервер соревнования и систему проверки решений. Укажите реальные адреса площадки.',custom:'Выберите цели и пороги под ваше соединение. Внешние адреса не подставляются автоматически.'};
  $('profile-help').textContent=descriptions[name];
  if(replace){$('target-inputs').replaceChildren();addTarget({label:'Шлюз площадки',mode:'icmp'});addTarget({label:name==='contest'?'Система проверки решений':'Сервер приёма потока',mode:'tcp',port:name==='contest'?443:1935});
    if(name==='contest')addTarget({label:'Сайт соревнования',mode:'tcp',port:443});
    $('setup-form').elements.latency_limit.value=name==='contest'?150:100;
  }
}
function openSetup(){
  if(state.snapshot?.active_id){toast('Сначала остановите текущую проверку');return;}
  $('setup-error').textContent='';$('setup').showModal();
}
async function start(event){
  event.preventDefault();const form=$('setup-form');const config=Object.fromEntries(new FormData(form));config.profile=state.profile;
  config.targets=[...$('target-inputs').children].map(row=>({label:row.querySelector('.target-label').value,host:row.querySelector('.target-host').value,mode:row.querySelector('.target-mode').value,family:row.querySelector('.target-family').value,address:row.querySelector('.target-address').value,port:row.querySelector('.target-port').value,required:row.querySelector('input[type=checkbox]').checked}));
  $('start').disabled=true;
  try{const result=await api('/api/start',config);state.session=result.id;state.target=null;state.all=false;state.hop=null;state.focus=null;$('focus-end').value='';$('setup').close();await refresh();toast('Проверка запущена. Можно закрыть вкладку — сбор продолжится.');}
  catch(error){$('setup-error').textContent=error.message;}finally{$('start').disabled=false;}
}
function metric(label,value,unit,note,bad=false){return `<div class="metric"><div class="metric-label">${label}</div><div class="metric-value ${bad?'bad':''}">${value}<small>${unit}</small></div><div class="metric-note">${escapeHTML(note)}</div></div>`;}
function render(){
  const snap=state.snapshot, session=snap.session;
  $('empty').classList.toggle('hidden',!!session);$('workspace').classList.toggle('hidden',!session);
  $('new-session').disabled=!!snap.active_id;
  if(!session){syncQuick();renderStream();return;}
  const live=session.status==='running';$('live-badge').className='live-badge'+(live?' running':'');$('live-badge').textContent=live?'● LIVE':'ОСТАНОВЛЕНО';
  $('session-name').textContent=session.config.name;$('session-status').textContent=labels[session.status];
  $('session-dot').style.background=live?'var(--green)':'var(--muted)';
  $('elapsed').textContent=duration(session.elapsed)+(live?' / осталось '+duration(session.remaining):'');
  $('stop').classList.toggle('hidden',!live||session.id!==snap.active_id);
  $('verdict').className='verdict '+snap.verdict;
  const titles={pending:live?'Проверка продолжается':'Недостаточно данных для оценки',risk:'Обнаружены риски стабильности',stable:'В выбранном периоде соединения в порогах'};
  $('verdict-title').textContent=titles[snap.verdict];$('verdict-symbol').textContent={pending:'◷',risk:'△',stable:'✓'}[snap.verdict];
  const required=snap.targets.filter(t=>t.required), risks=required.filter(t=>t.assessment.status==='risk'), pending=required.filter(t=>t.assessment.status==='pending');
  $('verdict-detail').textContent=risks.length?risks.map(t=>`${t.label}: ${t.assessment.reasons[0]}`).join(' · '):pending.length?pending.map(t=>`${t.label}: ${t.assessment.reasons[0]||'ждём данные'}`).join(' · '):'Это оценка сетевых проверок, а не гарантия качества трансляции или работы приложения.';
  const progress=Math.min(100,session.elapsed/(session.config.duration_minutes*60)*100);$('progress').value=progress;$('progress-text').textContent=`${fmt(progress,0)}% периода`;
  const target=selected();state.target=target.id;const s=target.stats,c=session.config;
  $('metrics').innerHTML=metric(target.mode==='tcp'?'Последний TCP connect':'Последний ICMP RTT',fmt(target.last?.latency),'мс',`${target.label} · среднее ${fmt(s.avg)} мс · P95 ${fmt(s.p95)} мс за сессию`,target.last?.latency>c.latency_limit)+metric(target.mode==='tcp'?'Неуспешные TCP-проверки':'Нет ICMP-ответа',fmt(s.loss,2),'%',`${s.failed} из ${s.attempted} · ошибки ОС: ${s.errors}`,s.loss>c.loss_limit)+metric('Jitter соседних ответов',fmt(s.jitter),'мс',`Средняя |RTT₂ − RTT₁| · порог ${c.jitter_limit} мс`,s.jitter>c.jitter_limit)+metric('Максимальная серия сбоев',fmt(s.max_streak,0),'проверок',`При интервале ${target.interval||c.interval} с · покрытие ${fmt(target.assessment.coverage)}%`,s.max_streak>c.burst_limit);
  $('target-count').textContent=snap.targets.length;
  $('target-rows').innerHTML=snap.targets.map(t=>{
    const latest=t.last?.status||'pending', stats=t.stats;
    return `<tr class="target-row ${t.id===state.target?'selected':''}" data-id="${t.id}" tabindex="0" aria-label="Выбрать ${escapeHTML(t.label)}"><td><strong>${escapeHTML(t.label)} ${t.required?'':'<span class="count">справочная</span>'}</strong><small>${escapeHTML(t.host)}${t.mode==='tcp'?':'+t.port:''} · ${t.mode.toUpperCase()} · ${escapeHTML(t.last?.ip||t.address||'IP ещё не определён')}</small></td><td><span class="badge ${latest}" title="${escapeHTML(t.last?.detail||t.assessment.reasons.join('; '))}">${labels[latest]}</span></td><td>${fmt(t.last?.latency)}</td><td>${fmt(stats.avg)}</td><td class="${stats.p95>c.latency_limit?'bad':''}">${fmt(stats.p95)}</td><td class="${stats.loss>c.loss_limit?'bad':''}">${fmt(stats.loss,2)}% ${stats.errors?'<span class="bad">+ '+stats.errors+' ошибок</span>':''}</td><td class="${stats.jitter>c.jitter_limit?'bad':''}">${fmt(stats.jitter)}</td><td>${stats.count}</td></tr>`;
  }).join('');
  const hop=target.hops?.find(h=>h.node===state.hop), plotted=hop||target;
  if(state.hop&&!hop)state.hop=null;
  $('chart-title').textContent=target.label+(hop?' · узел '+hop.hop:'')+' · задержка';
  $('chart-subtitle').textContent=`${target.mode==='tcp'?'TCP connect':'ICMP RTT'} · ${hop?.ip||target.window_last?.ip||target.address||target.host} · ${hop?'фактический интервал '+fmt(hop.interval,2)+' с':'Сетевые измерения · итог для RTMP выше'}`;
  const w=hop?.stats||target.window_stats;
  $('chart-footer').textContent=`Окно ${date(session.analysis_start)} — ${date(session.analysis_end)}: ${w.count} измерений · min ${fmt(w.min)} · avg ${fmt(w.avg)} · max ${fmt(w.max)} мс · нет ответа ${fmt(w.loss,2)}% · ошибки ${w.errors}. Разрывы означают отсутствие измерений или ответа. Общая оценка выше — за всю сессию.`;
  renderRoute(target);if(typeof renderPlotter==='function')renderPlotter(target);drawChart();renderEvents();
}
function drawChart(){
  if(typeof drawPlotterChart==='function'){drawPlotterChart();return;}
  const target=selected();if(!target)return;
  const canvas=$('chart'), rect=canvas.getBoundingClientRect(), ratio=window.devicePixelRatio||1;
  if(rect.width<1)return;canvas.width=Math.round(rect.width*ratio);canvas.height=Math.round(280*ratio);
  const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);const width=rect.width,height=280;
  const left=52,right=16,top=20,bottom=35,pw=width-left-right,ph=height-top-bottom;
  const session=state.snapshot.session, end=session.analysis_end,start=session.analysis_start;
  const plotted=target.hops?.find(h=>h.node===state.hop)||target;
  const points=plotted.graph, limit=session.config.latency_limit;
  const maximum=Math.max(5,...points.filter(p=>p.latency!=null).map(p=>p.latency*1.2));
  const ymax=Math.ceil(maximum/5)*5;
  const x=ts=>left+(ts-start)/Math.max(1,end-start)*pw,y=v=>top+ph*(1-v/ymax);
  ctx.font='10px ui-monospace, monospace';ctx.textAlign='right';ctx.fillStyle='#8fa4ba';ctx.strokeStyle='#263548';ctx.lineWidth=1;
  for(let i=0;i<=4;i++){const yy=top+ph*i/4;ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(width-right,yy);ctx.stroke();ctx.fillText(fmt(ymax*(1-i/4),ymax<=20?1:0),left-9,yy+3);}
  ctx.textAlign='center';for(let i=0;i<=4;i++){const xx=left+pw*i/4;ctx.fillText(clock(start+(end-start)*i/4),xx,height-12);}
  if(limit<=ymax){ctx.setLineDash([5,5]);ctx.strokeStyle='#d2aa65';ctx.beginPath();ctx.moveTo(left,y(limit));ctx.lineTo(width-right,y(limit));ctx.stroke();ctx.setLineDash([]);}else{ctx.fillStyle='#d2aa65';ctx.textAlign='right';ctx.fillText(`Порог ${limit} мс выше шкалы`,width-right,12);}
  ctx.strokeStyle='#71e2bd';ctx.lineWidth=1.7;let last=null;
  for(const p of points){if(p.status==='ok'&&p.latency!=null){if(last&&!p.break_before&&p.ts-last.ts<=(plotted.interval||session.config.interval)*(plotted.graph_step||1)*3+2){ctx.beginPath();ctx.moveTo(x(last.ts),y(last.latency));ctx.lineTo(x(p.ts),y(p.latency));ctx.stroke();}else{ctx.beginPath();ctx.arc(x(p.ts),y(p.latency),2,0,Math.PI*2);ctx.fillStyle='#71e2bd';ctx.fill();}last=p;}else{last=null;ctx.strokeStyle=p.status==='error'?'#6aa9ff':'#ff7c89';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x(p.ts),top+ph-22);ctx.lineTo(x(p.ts),top+ph);ctx.stroke();ctx.strokeStyle='#71e2bd';ctx.lineWidth=1.7;}}
  if(!points.length){ctx.fillStyle='#93a5ba';ctx.textAlign='center';ctx.font='13px -apple-system, sans-serif';ctx.fillText('Ожидаем первые измерения',width/2,130);}
  canvas.onmousemove=event=>{const fraction=Math.max(0,Math.min(1,(event.clientX-rect.left-left)/pw));const ts=start+fraction*(end-start);const point=points.reduce((a,p)=>!a||Math.abs(p.ts-ts)<Math.abs(a.ts-ts)?p:a,null);canvas.title=point?`${clock(point.ts)} · ${labels[point.status]} · ${fmt(point.latency)} мс${point.detail?' · '+point.detail:''}`:'';};
}
function renderRoute(target){
  $('trace').disabled=target.tracing||(['icmp','route'].includes(target.mode)&&state.snapshot.session.status==='running');
  $('hop-table').innerHTML=target.hops?.length?`<table><thead><tr><th>Узел / IP</th><th>Последний в окне</th><th>Min / Avg / Max, мс</th><th>Нет ответа</th><th>Jitter</th><th>Замеры</th></tr></thead><tbody>${target.hops.map(h=>`<tr data-node="${escapeHTML(h.node)}" tabindex="0" class="${h.node===state.hop?'selected':''}"><td>${h.hop}. ${escapeHTML(h.ip||'Адрес неизвестен')}${h.destination?' ◎ цель':''}</td><td>${h.last.status==='ok'?fmt(h.last.latency)+' мс':labels[h.last.status]}</td><td>${fmt(h.stats.min)} / ${fmt(h.stats.avg)} / ${fmt(h.stats.max)}</td><td>${fmt(h.stats.loss,1)}%${h.stats.errors?' + '+h.stats.errors+' ошибок':''}</td><td>${fmt(h.stats.jitter)}</td><td>${h.stats.count}</td></tr>`).join('')}</tbody></table>`:'<p class="form-help">История узлов появится в режиме «Маршрут». Разовая трассировка показана ниже.</p>'; $('trace').textContent=['icmp','route'].includes(target.mode)&&state.snapshot.session.status==='running'?'Маршрут · авто':target.tracing?'Выполняется…':'↗ Проверить маршрут';
  if(!target.route&&!target.current_route){$('route').className='route-empty';$('route').textContent=target.tracing?'Выполняем ICMP-трассировку; молчание узлов увеличивает время ожидания…':'Запустите трассировку для выбранной цели.';return;}
  const route=target.current_route||target.route;$('route').className='route-visualization';
  const hops=route.data.hops||[];
  $('route').innerHTML=`<p class="route-summary">${target.current_route?'Новый снимок вне периода сессии · ':''}${escapeHTML(target.label)} · ${escapeHTML(route.data.message)} · ${clock(route.ts)} · ${escapeHTML(route.data.destination||'')}</p>`+(hops.length?`<div class="chart-legend"><span><i class="legend-line"></i> Ответ узла</span><span><i class="legend-error"></i> Конечная цель</span><span>○ RTT не измерен</span></div><div class="route-scroll">${routeGraph(hops,route.data.destination,state.snapshot.session.config.latency_limit)}</div>`:'<p class="route-empty">Нет данных узлов для построения графика.</p>');
}
function renderEvents(){
  const snap=state.snapshot;const byId=Object.fromEntries(snap.targets.map(t=>[t.id,t.label]));$('event-count').textContent=snap.events.length>=200?'200+':snap.events.length;
  $('events').innerHTML=snap.events.map(e=>`<div class="event ${escapeHTML(e.level)}"><span class="dot"></span><div class="event-text">${escapeHTML(e.message)}<small>${clock(e.ts)}${e.target?' · '+escapeHTML(byId[e.target]||''):''}</small></div></div>`).join('')||'<p class="footnote">Пока событий нет.</p>';
}
async function refresh(){
  try{const query=new URLSearchParams({window:state.window,...(state.session?{session:state.session}:{}),...(state.focus?{end:state.focus}:{})});const snapshot=await api('/api/state?'+query);state.snapshot=snapshot;state.connected=true;render();}
  catch(error){if(state.connected){toast('Нет связи с локальным сервером. Проверьте, что приложение запущено.');state.connected=false;}$('live-badge').textContent='НЕТ СВЯЗИ';$('live-badge').className='live-badge';renderStream();}
}
async function polling(){
  const started=performance.now();
  await refresh();
  const seconds=Number(state.snapshot?.session?.config?.interval||$('quick-interval')?.value||1);
  const intervalMs=Number.isFinite(seconds)&&seconds>0?seconds*1000:1000;
  setTimeout(polling,Math.max(0,intervalMs-(performance.now()-started)));
}
async function history(){
  try{const sessions=await api('/api/history');$('history-panel').classList.remove('hidden');$('history-list').innerHTML=sessions.map(s=>`<div class="history-item"><div><strong>${escapeHTML(s.name)}</strong><small>${date(s.started)} · ${s.targets} целей · ${labels[s.status]}</small></div><button class="quiet open-history" data-session="${s.id}">Открыть →</button></div>`).join('')||'<p class="form-help">Сохранённых проверок пока нет.</p>';}catch(error){toast(error.message);}
}
async function download(format){
  try{const response=await fetch(`/api/export.${format}?session=${state.snapshot.session.id}`,{headers:{'X-Local-Token':token}});if(!response.ok)throw new Error((await response.json()).error);saveBlob(await response.blob(),`network-${state.snapshot.session.id.slice(0,8)}.${format}`);}catch(error){toast(error.message);}
}
function saveBlob(blob,name){const link=document.createElement('a');const url=URL.createObjectURL(blob);link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function reportHTML(snap){
  const session=snap.session,c=session.config;
  return `<div class="report-body"><h3>${escapeHTML(c.name)}</h3><p>${escapeHTML(date(session.started))} — ${escapeHTML(date(session.ended||Date.now()/1000))}<br>Длительность: ${duration(session.elapsed)} · состояние: ${labels[session.status]}<br>Общая оценка обязательных целей: <strong>${labels[snap.verdict]}</strong></p><p>Интервал: ${c.interval} с · таймаут: ${c.timeout} с · заданный период: ${c.duration_minutes} мин.<br>Пороги: неуспешные ≤ ${c.loss_limit}%, P95 ≤ ${c.latency_limit} мс, jitter ≤ ${c.jitter_limit} мс, серия сбоев ≤ ${c.burst_limit}.</p><div class="table-wrap"><table><thead><tr><th>Цель</th><th>Проверки</th><th>Неуспешные</th><th>Avg / P95, мс</th><th>Jitter, мс</th><th>Ошибки</th><th>Оценка</th></tr></thead><tbody>${snap.targets.map(t=>`<tr><td>${escapeHTML(t.label)}<br>${escapeHTML(t.host)}${t.mode==='tcp'?':'+t.port:''} · ${t.mode.toUpperCase()}${t.required?' · обязательная':''}</td><td>${t.stats.count}<br>покрытие ${fmt(t.assessment.coverage)}%</td><td>${fmt(t.stats.loss,2)}%<br>серия: ${t.stats.max_streak}</td><td>${fmt(t.stats.avg)} / ${fmt(t.stats.p95)}</td><td>${fmt(t.stats.jitter)}</td><td>${t.stats.errors}</td><td>${labels[t.assessment.status]}</td></tr>`).join('')}</tbody></table></div><h3>Причины оценки</h3><ul>${snap.targets.map(t=>`<li><strong>${escapeHTML(t.label)}:</strong> ${escapeHTML(t.assessment.reasons.join('; '))}</li>`).join('')}</ul><h3>События (последние 200)</h3><ul>${snap.events.slice().reverse().map(e=>`<li>${date(e.ts)} · ${escapeHTML(snap.targets.find(t=>t.id===e.target)?.label||'Сессия')}: ${escapeHTML(e.message)}</li>`).join('')}</ul><h3>Ограничения измерений</h3><p>Все проверки выполнены с компьютера, где запущено приложение. ICMP: RTT и доля отсутствующих ответов, TCP: время установки соединения и доля неуспешных соединений. Ошибки DNS/ОС исключены из знаменателя неуспешных проверок и блокируют положительную оценку. Значение Windows &lt;1 мс записывается как верхняя граница 1 мс. P95: ближайший ранг; jitter: средняя абсолютная разница соседних успешных измерений.</p><p>Оценка не подтверждает достаточную пропускную способность, работу HTTPS/RTMP/SRT или качество видеопотока. Сон и пропуски сбора могут сделать данные недостаточными. ICMP-маршрут поддерживает IPv4/IPv6; циклы последовательны, их длительность зависит от ответов узлов. Молчание промежуточного узла не доказывает потерю трафика. Обратный путь может отличаться. Хорошие результаты не гарантируют отсутствие будущих сбоев. Проверку нужно проводить на реальном интерфейсе, с фактическими адресами и нагрузкой события.</p><p>LinkRadar ${escapeHTML(snap.version||'1.0.0')} · время отображено в часовом поясе ${escapeHTML(Intl.DateTimeFormat().resolvedOptions().timeZone)}. В CSV есть UTC и Unix timestamp.</p></div>`;
}
$('focus-end').onchange=async()=>{const value=$('focus-end').value;state.focus=value?new Date(value).getTime()/1000:null;await refresh();};
$('follow-live').onclick=async()=>{state.focus=null;$('focus-end').value='';await refresh();};
$('final-graph').onclick=()=>{state.hop=null;render();};
$('hop-table').onclick=event=>{const row=event.target.closest('[data-node]');if(row){state.hop=row.dataset.node;render();}};
$('hop-table').onkeydown=event=>{if(event.key==='Enter'){event.target.closest('[data-node]')?.click();}};
$('export-hops').onclick=async()=>{try{const r=await fetch('/api/hops.csv?session='+state.snapshot.session.id,{headers:{'X-Local-Token':token}});if(!r.ok)throw new Error('Не удалось выгрузить узлы');saveBlob(await r.blob(),'route-hops.csv');}catch(e){toast(e.message);}};
let reportSnapshot;
$('new-session').onclick=openSetup;$('first-session').onclick=openSetup;
$('close-setup').onclick=$('cancel-setup').onclick=()=>$('setup').close();
$('setup-form').onsubmit=start;$('add-target').onclick=()=>addTarget();
document.querySelectorAll('.profile').forEach(b=>b.onclick=()=>profile(b.dataset.profile,true));
$('stop').onclick=async()=>{try{await api('/api/stop',{});toast('Остановка запрошена; завершаем текущие измерения.');await refresh();}catch(error){toast(error.message);}};
$('window').onchange=async event=>{state.window=Number(event.target.value);await refresh();};
$('target-rows').onclick=event=>{const row=event.target.closest('[data-id]');if(row){state.all=false;state.target=row.dataset.id;state.hop=null;render();}};
$('target-rows').onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();event.target.closest('[data-id]')?.click();}};
$('trace').onclick=async()=>{const t=selected();try{await api('/api/trace',{session:state.snapshot.session.id,target:t.id});await refresh();}catch(error){toast(error.message);}};
$('comment-form').onsubmit=async event=>{event.preventDefault();try{await api('/api/comment',{session:state.snapshot.session.id,message:$('comment').value});$('comment').value='';await refresh();}catch(error){toast(error.message);}};
$('history-nav').onclick=history;$('close-history').onclick=()=>$('history-panel').classList.add('hidden');
$('monitor-nav').onclick=async()=>{state.session=state.snapshot?.active_id||null;state.target=null;state.hop=null;state.all=false;state.focus=null;$('focus-end').value='';$('history-panel').classList.add('hidden');await refresh();};
$('history-list').onclick=async event=>{const button=event.target.closest('.open-history');if(button){state.session=button.dataset.session;state.target=null;state.hop=null;state.all=false;state.focus=null;$('focus-end').value='';$('history-panel').classList.add('hidden');await refresh();}};
$('export-csv').onclick=()=>download('csv');$('export-json').onclick=()=>download('json');
$('export-report').onclick=()=>{reportSnapshot=structuredClone(state.snapshot);$('report-content').innerHTML=reportHTML(reportSnapshot);$('report-dialog').showModal();};
$('close-report').onclick=()=>$('report-dialog').close();
$('download-report').onclick=()=>{
  const body=reportHTML(reportSnapshot);
  const html=`<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Network Watch — отчёт</title><style>body{font:14px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 24px;color:#183044;background:#fff}h3{margin-top:30px}p,li{line-height:1.7}table{border-collapse:collapse;width:100%;font-size:12px}td,th{border:1px solid #c8d2dc;padding:10px;text-align:left}th{background:#edf3f7}.table-wrap{overflow:auto}@media print{body{margin:0}tr{break-inside:avoid}}</style>${body}</html>`;
  saveBlob(new Blob([html],{type:'text/html;charset=utf-8'}),'network-report-'+reportSnapshot.session.id.slice(0,8)+'.html');
};
window.addEventListener('resize',()=>drawChart());
profile('broadcast',true);
api('/api/system').then(info=>{$('machine').replaceChildren(document.createTextNode(info.hostname));const small=document.createElement('small');small.textContent=info.os+' · локальный сбор';$('machine').append(small);$('version').textContent='VERSION '+info.version;}).catch(()=>{});
polling();

'use strict';
// Dense route workspace inspired by the user's reference, using recorded data only.
const routePanel=document.querySelector('.route-panel'), chartPanel=document.querySelector('.chart-panel');
const workspace=$('workspace');
const tabs=document.createElement('div');tabs.id='plotter-tabs';tabs.setAttribute('role','tablist');workspace.prepend(tabs);
const toolbar=document.createElement('div');toolbar.className='plotter-toolbar';
toolbar.innerHTML='<input id="plotter-address" aria-label="Адрес сервера" placeholder="IP или имя сервера"><button id="apply-address" class="quiet" title="Сменить адрес и начать новую сессию; история сохранится">Применить</button><span id="plotter-ip"></span><button id="plotter-pause" class="quiet" aria-label="Пауза сбора">Ⅱ</button><span id="plotter-cadence"></span><button id="plotter-setup" class="quiet">＋ Цель / сессия</button><button id="plotter-trace" class="quiet">↗ Маршрут</button><span class="scale-key"><i></i>100 мс · 200 мс</span>';
$('empty').before(toolbar);tabs.after(routePanel);routePanel.after(chartPanel);
const detail=document.createElement('details');detail.id='diagnostic-details';detail.innerHTML='<summary>Сессия, оценка, отчёты и журнал</summary>';
chartPanel.after(detail);
for(const selector of ['.session-strip','#verdict','#metrics','.targets-panel','.bottom-grid']){const node=document.querySelector(selector);if(node)detail.append(node);}
const jitter=document.createElement('canvas');jitter.id='jitter-chart';jitter.height=48;jitter.setAttribute('aria-label','Сглаженный jitter в миллисекундах');$('chart').before(jitter);
const readout=document.createElement('div');readout.id='chart-readout';readout.setAttribute('role','status');$('jitter-chart').before(readout);
const scale=document.createElement('select');scale.id='chart-scale';scale.setAttribute('aria-label','Шкала задержки');scale.innerHTML='<option value="0">Шкала: авто</option><option value="50">0–50 мс</option><option value="100">0–100 мс</option><option value="300">0–300 мс</option><option value="1000">0–1000 мс</option>';document.querySelector('.chart-controls').append(scale);scale.onchange=()=>drawChart();
const interval=document.createElement('select');interval.id='quick-interval';interval.setAttribute('aria-label','Интервал опроса');interval.innerHTML='<option value="0.5">0,5 сек</option><option value="1" selected>1 сек</option><option value="2">2 сек</option>';$('plotter-address').after(interval);
const options=document.createElement('details');options.id='view-options';options.innerHTML='<summary>Настройки</summary>';document.querySelector('header').append(options);options.append(document.querySelector('.sidebar'));options.append($('new-session'));options.append(document.querySelector('.chart-controls'));
$('history-nav').remove();
const streamPanel=document.createElement('section');streamPanel.id='stream-panel';toolbar.after(streamPanel);
streamPanel.innerHTML=`<details id="stream-profile"><summary id="stream-profile-title">RTMP · 8 Мбит/с · 1080p30</summary><div class="stream-fields"><label>Формат<select id="stream-format"><option>1080p25</option><option selected>1080p30</option><option>1080p50</option><option>1080p60</option></select></label><label>Видео, Мбит/с<input id="stream-bitrate" type="number" value="8" min="0.1" max="200" step="0.1"></label><label>Аудио, кбит/с<input id="stream-audio" type="number" value="160" min="0" max="1536"></label><label>TCP-порт<input id="stream-port" type="number" value="1935" min="1" max="65535"></label><label>Отдача до сервера, Мбит/с · вручную<input id="stream-upload" type="number" min="0" step="0.1" placeholder="Не измерена"></label><label>Сетевые dropped frames · вручную<input id="stream-dropped" type="number" min="0" step="1" placeholder="Не указаны"></label><label>Переподключения · вручную<input id="stream-reconnects" type="number" min="0" step="1" placeholder="Не указаны"></label></div><p class="stream-note">Отдачу и сбои укажите по отдельному тесту или статистике OBS/vMix. FPS не задаёт порог jitter; при одинаковом битрейте сетевые требования близки. Порт применяется при следующем запуске.</p></details><div id="stream-summary" aria-live="polite"></div><div id="stream-metrics"></div><p class="stream-note">Укажите сервер приёма вашей трансляции. ICMP jitter — колебания RTT. TCP connect проверяет доступность порта, без RTMP handshake и передачи видео. Пропускная способность и качество эфира автоматически не измеряются.</p>`;
function streamProfile(){
  const value=(id,fallback)=>{const raw=$(id).value;if(raw==='')return fallback;const n=Number(raw);return Number.isFinite(n)?n:fallback;};
  return {format:$('stream-format').value,bitrate:Math.max(.1,value('stream-bitrate',8)),audio:Math.max(0,value('stream-audio',160)),port:Math.max(1,Math.min(65535,Math.round(value('stream-port',1935)))),upload:value('stream-upload',null),dropped:value('stream-dropped',null),reconnects:value('stream-reconnects',null)};
}
function renderStream(){
  const sid=state.snapshot?.session?.id;if(state.streamObservationSession!==sid){state.streamObservationSession=sid;for(const field of ['upload','dropped','reconnects'])$('stream-'+field).value='';try{const saved=JSON.parse(localStorage.getItem('network-stream-observations')||'null');if(saved?.session===sid)for(const field of ['upload','dropped','reconnects'])if(saved[field]!=null)$('stream-'+field).value=saved[field];}catch(_){}}
  const p=streamProfile(),a=Streaming.analyze(state.snapshot,{...p,upload:null,dropped:null,reconnects:null});
  $('stream-profile-title').textContent=`RTMP · ${fmt(p.bitrate)} Мбит/с · ${p.format}`;
  const ok=a.level==='good'&&state.connected;
  $('stream-summary').className=ok?'good':'bad';
  $('stream-summary').title=!state.connected?'Нет связи с приложением':a.reasons[0]||(ok?'Сетевые пробы без сбоев; качество эфира не измеряется':a.missing[0]||'Недостаточно данных');
  $('stream-summary').innerHTML=`<div class="stream-verdict"><span class="status-symbol">${ok?'✓':'!'}</span><strong>${ok?'OK':'ALARM'}</strong></div>`;
  const ping=a.ping,tcp=a.tcp;
  const card=(label,value,unit,tone)=>`<div class="metric stream-${tone}"><div class="stream-card-head">${label}</div><div class="metric-value">${value}<small>${unit}</small></div></div>`;
  $('stream-metrics').innerHTML=card('Jitter',fmt(a.currentJitter),'мс','neutral')+
    card('Ping без ответа',fmt(ping?.loss,2),'%',ping?.failed||ping?.errors?'warn':ping?.attempted?'good':'neutral')+
    card('Порт RTMP',tcp?.failed||tcp?.errors?'Сбои':tcp?.attempted?'Доступен':'Не проверен','',tcp?.failed||tcp?.errors?'warn':tcp?.attempted?'good':'neutral');

}
function rememberStream(){
  const p=streamProfile();try{localStorage.setItem('network-stream-profile',JSON.stringify({format:p.format,bitrate:p.bitrate,audio:p.audio,port:p.port}));if(state.snapshot?.session)localStorage.setItem('network-stream-observations',JSON.stringify({session:state.snapshot.session.id,upload:p.upload,dropped:p.dropped,reconnects:p.reconnects}));}catch(_){}
}
try{const p=JSON.parse(localStorage.getItem('network-stream-profile')||'null');if(p){for(const name of ['format','bitrate','audio','port'])if(p[name]!=null)$('stream-'+name).value=p[name];}}catch(_){}
streamPanel.oninput=()=>{rememberStream();renderStream();};

const routeDetails=document.createElement('section');routeDetails.id='route-details';routeDetails.innerHTML='<h2 class="route-title">Маршрут и статистика узлов</h2>';routePanel.before(routeDetails);routeDetails.append(routePanel);
const range=document.createElement('div');range.id='route-focus';$('hop-table').after(range);
$('plotter-address').oninput=()=>{state.addressDirty=true;syncQuick();};
$('plotter-address').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();quickAction();}if(e.key==='Escape'){state.addressDirty=false;render();}};
$('apply-address').onclick=()=>quickAction();
interval.onchange=()=>{state.intervalDirty=true;const snap=state.snapshot;if(snap?.active_id&&snap.active_id===snap.session?.id&&!snap.paused)quickAction(true);};
function syncQuick(){
  const snap=state.snapshot,target=selected(),running=snap?.active_id&&snap.active_id===snap.session?.id&&!snap.paused;
  const same=target&&$('plotter-address').value.trim()===target.host&&!state.intervalDirty;
  $('apply-address').textContent=state.switching?'Подождите…':'Пуск / Стоп';
  $('apply-address').classList.toggle('is-running',!!running);
  $('apply-address').setAttribute('aria-pressed',String(!!running));
  $('apply-address').disabled=!!state.switching;$('plotter-address').disabled=!!state.switching;interval.disabled=!!state.switching;
  $('apply-address').title=running&&same?'Остановить проверку':'Запустить проверку';
}
async function waitForStop(id){
  await api('/api/stop',{});const deadline=Date.now()+60000;
  while(true){const current=await api('/api/state');if(!current.active_id)return;if(current.active_id!==id)throw new Error('Другая проверка уже запущена');if(Date.now()>deadline)throw new Error('Остановка ещё выполняется');await new Promise(resolve=>setTimeout(resolve,250));}
}
async function quickAction(forceStart=false){
  forceStart=forceStart===true;
  if(state.switching)return;
  const snap=state.snapshot,target=selected(),rawHost=$('plotter-address').value.trim(),seconds=Number(interval.value);
  let endpoint;try{endpoint=Streaming.endpoint(rawHost,streamProfile().port);}catch(e){toast(e.message);return;}const host=endpoint.host;
  if(!host){toast('Введите IP или имя сервера');$('plotter-address').focus();return;}
  if(snap?.active_id&&snap.active_id!==snap.session?.id){toast('Откройте текущую проверку в настройках через «Мониторинг»');return;}
  const same=target&&host===target.host&&seconds===snap.session.config.interval;
  state.switching=true;syncQuick();
  try{
    if(snap?.active_id&&!forceStart){
      await waitForStop(snap.active_id);
    }else{
      await api('/api/resolve?'+new URLSearchParams({host}));
      if(snap?.active_id)await waitForStop(snap.active_id);
      const result=await api('/api/start',{name:'RTMP · '+host,profile:'broadcast',duration_minutes:1440,interval:seconds,timeout:Math.min(seconds,1),route_interval:seconds,max_hops:20,targets:[{label:host,host,mode:'icmp',family:'auto',required:true},{label:'Порт приёма RTMP',host,mode:'tcp',port:endpoint.port,family:'auto',required:true}]});
      $('stream-port').value=endpoint.port;state.session=result.id;state.target=null;state.all=false;state.hop=null;state.focus=null;state.probe=null;state.addressDirty=false;state.addressTarget=null;state.intervalDirty=false;$('focus-end').value='';
      try{const p=streamProfile();localStorage.setItem('network-stream-profile',JSON.stringify({format:p.format,bitrate:p.bitrate,audio:p.audio,port:p.port}));}catch(_){}
      if(forceStart)toast('Интервал изменён. Новая сессия; предыдущие измерения сохранены в истории.');
    }
  }catch(e){toast(e.message);}finally{state.switching=false;await refresh();syncQuick();}
}
$('plotter-setup').onclick=openSetup;$('plotter-trace').onclick=()=>$('trace').click();
const periodControl=document.createElement('label');periodControl.className='graph-period';periodControl.textContent='Период';
document.querySelector('label[for="window"]').remove();
$('window').innerHTML='<option value="60">1 мин</option><option value="300">5 мин</option><option value="600">10 мин</option><option value="900">15 мин</option><option value="1200">20 мин</option><option value="1800">30 мин</option><option value="3600">1 час</option><option value="7200">2 часа</option><option value="21600">6 часов</option><option value="43200">12 часов</option><option value="86400">24 часа</option>';
$('window').value=state.window;$('window').setAttribute('aria-label','Период графика');periodControl.append($('window'));chartPanel.querySelector('.section-heading').append(periodControl);
tabs.onclick=e=>{if(e.target.closest('[data-all]')){state.all=true;render();return;}const b=e.target.closest('[data-target]');if(b){state.all=false;state.target=b.dataset.target;state.hop=null;render();}};
const overviewPanel=document.querySelector('.targets-panel');toolbar.after(overviewPanel);overviewPanel.classList.add('hidden');
$('plotter-pause').onclick=async()=>{try{await api('/api/pause',{session:state.snapshot.session.id});await refresh();}catch(e){toast(e.message);}};
function plotterRows(target){
  if(target.hops?.length)return target.hops.map(h=>({...h,name:h.destination?target.host:(h.name||'—'),snapshot:false}));
  const route=target.current_route||target.route;
  if(route?.data.hops?.length)return route.data.hops.map(h=>({hop:h.hop,node:null,ip:h.ip,name:h.ip===route.data.destination?target.host:'—',last:{latency:h.latency,status:h.status||(h.latency!=null?'ok':'timeout')},stats:{count:null,min:null,avg:null,max:null,loss:null},snapshot:true,destination:h.ip===route.data.destination}));
  return [{hop:'—',node:null,ip:target.window_last?.ip||target.address||null,name:target.host,last:target.window_last||{},stats:target.window_stats,snapshot:false,destination:true}];
}
function latencyCell(row,maximum){
  const s=row.stats,x=v=>Math.max(0,Math.min(100,v/maximum*100)),current=row.last?.latency;
  return `<svg class="hop-latency" viewBox="0 0 1000 30" preserveAspectRatio="none" aria-label="${escapeHTML('RTT: среднее '+fmt(s.avg)+', последний '+fmt(current)+' мс')}"><rect width="${x(100)*10}" height="30" fill="#2f6031"/><rect x="${x(100)*10}" width="${(x(200)-x(100))*10}" height="30" fill="#786220"/><rect x="${x(200)*10}" width="${(100-x(200))*10}" height="30" fill="#743a3c"/>${s.min!=null?`<path d="M ${x(s.min)*10} 15 H ${x(s.max)*10} M ${x(s.min)*10} 11 V 19 M ${x(s.max)*10} 11 V 19" stroke="#b5b5b5" fill="none"/>`:''}${s.avg!=null?`<ellipse cx="${x(s.avg)*10}" cy="15" rx="9" ry="5" stroke="#ff514e" fill="none"/>`:''}${current!=null?`<path d="M ${x(current)*10-7} 11 l 14 8 M ${x(current)*10-7} 19 l 14 -8" stroke="#00c8ef" fill="none"/>`:''}</svg>`;
}
function renderPlotter(target){
  const snap=state.snapshot,session=snap.session;renderStream();
  $('plotter-title').textContent=target.host+' — LinkRadar';document.title=target.host+' — LinkRadar';
  overviewPanel.classList.toggle('hidden',!state.all);routePanel.classList.toggle('hidden',!!state.all);chartPanel.classList.toggle('hidden',!!state.all);
  $('plotter-pause').disabled=session.status!=='running';$('plotter-pause').textContent=snap.paused?'▶':'Ⅱ';$('plotter-pause').title=snap.paused?'Продолжить сбор':'Пауза после текущей пробы; таймер сессии продолжается';
  tabs.innerHTML='<button role="tab" data-all aria-selected="'+!!state.all+'" class="'+(state.all?'active':'')+'">Все цели</button>'+snap.targets.map(t=>`<button role="tab" aria-selected="${!state.all&&t.id===target.id}" data-target="${t.id}" class="${!state.all&&t.id===target.id?'active':''}">${escapeHTML(t.host)} <small>${t.mode==='route'?'маршрут':t.mode==='icmp'?'ICMP + маршрут':t.mode.toUpperCase()}</small></button>`).join('');
  if(state.addressTarget!==target.id){state.addressTarget=target.id;state.addressDirty=false;}
  if(!state.addressDirty&&!state.switching)$('plotter-address').value=target.host;
  $('plotter-ip').textContent=target.window_last?.ip||target.address||target.last?.ip||'ожидаем IP';
  if(!state.intervalDirty){const v=String(session.config.interval);if(['0.5','1','2'].includes(v))interval.value=v;}syncQuick();
  $('plotter-cadence').textContent=`Задано: ${fmt(target.interval||session.config.interval,2)} с · ${snap.paused?'Пауза':labels[session.status]}`;
  $('plotter-trace').disabled=$('trace').disabled;$('plotter-trace').textContent=['icmp','route'].includes(target.mode)&&session.status==='running'?'Маршрут · авто':'↗ Маршрут';
  $('plotter-setup').disabled=!!snap.active_id;
  const rows=plotterRows(target),maximum=Math.max(50,...rows.flatMap(r=>[r.stats.max||0,r.last?.latency||0]).map(v=>Math.ceil(v/50)*50));
  $('hop-table').innerHTML=`<table class="plotter-table"><colgroup><col class="hop-col"><col class="count-col"><col class="ip-col"><col class="name-col"><col class="num-col"><col class="num-col"><col class="num-col"><col class="num-col"><col class="latency-col"></colgroup><thead><tr><th>Hop</th><th>Count</th><th>IP</th><th>Name</th><th>Avg</th><th>Min</th><th>Cur</th><th>PL%</th><th class="latency-head"><span>0 мс</span>Latency<span>${maximum} мс</span></th></tr></thead><tbody>${rows.map(r=>`<tr ${r.node?'data-node="'+escapeHTML(r.node)+'" tabindex="0"':''} class="${(r.node&&r.node===state.hop)||(state.hop==null&&r.destination)?'selected':''}"><td><span class="hop-dot ${r.last?.status==='ok'?'answered':''}">${r.hop}</span></td><td>${fmt(r.stats.count,0)}</td><td title="${escapeHTML(r.ip||'Адрес неизвестен')}">${escapeHTML(r.ip||'—')}</td><td>${escapeHTML(r.name)}</td><td>${fmt(r.stats.avg)}</td><td>${fmt(r.stats.min)}</td><td>${r.last?.status==='timeout'?'*':fmt(r.last?.latency)}</td><td>${fmt(r.stats.loss,1)}</td><td class="latency-cell">${latencyCell(r,maximum)}</td></tr>`).join('')}</tbody></table>`;
  const avgLine=rows.map((r,i)=>r.stats.avg!=null?`${r.stats.avg/maximum*1000},${i*30+15}`:null).filter(Boolean).join(' ');
  if(avgLine)$('hop-table').insertAdjacentHTML('beforeend',`<svg class="avg-route-line" viewBox="0 0 1000 ${rows.length*30}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${avgLine}" fill="none" stroke="#ff514e" stroke-width="1"/></svg>`);
  const line=$('hop-table').querySelector('.avg-route-line');if(line)line.style.height=(rows.length*24)+'px';
  $('route-focus').textContent=`${rows.some(r=>r.snapshot)?'Разовый архивный снимок: история узлов не собиралась. Новые ICMP-сессии собирают её автоматически.':'Статистика узлов за выбранный фокус · RTT, мс · PL% = нет ответа на зонд, не потери проходящего трафика.'}  Фокус: ${clock(session.analysis_start)} — ${clock(session.analysis_end)}`;
}
function drawPlotterChart(){
  const target=selected();if(!target)return;
  const p=target.hops?.find(h=>h.node===state.hop)||{graph:target.graph,stats:target.window_stats,interval:target.interval,graph_step:target.graph_step};
  const points=p.graph||[],session=state.snapshot.session;
  const canvas=$('chart'),rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
  const height=230,width=rect.width;if(width<1)return;
  canvas.width=width*ratio;canvas.height=height*ratio;const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);
  const left=45,right=36,top=12,bottom=28,pw=width-left-right,ph=height-top-bottom;
  const end=session.analysis_end,start=end-state.window;
  const peak=Math.max(1,...points.map(q=>q.latency||0));
  const nice=v=>{const power=10**Math.floor(Math.log10(v));return [1,2,2.5,5,10].find(n=>n*power>=v)*power;};
  const tick=nice(Math.max(10,peak*1.15)/5),max=Number($('chart-scale').value)||tick*5,grid=Number($('chart-scale').value)?max/5:tick;
  const x=t=>left+(t-start)/(end-start)*pw,y=v=>top+ph*(1-v/max);
  ctx.fillStyle='#0e1420';ctx.fillRect(0,0,width,height);
  for(const [lo,hi,color] of [[0,100,'#314833'],[100,200,'#51492b'],[200,max,'#513437']]){ctx.fillStyle=color;const low=Math.min(max,lo),high=Math.min(max,hi);ctx.fillRect(left,y(high),pw,Math.max(0,y(low)-y(high)));}
  if(session.started>start){ctx.fillStyle='#18191b99';ctx.fillRect(left,top,Math.min(pw,x(session.started)-left),ph);}
  ctx.font='11px ui-monospace,monospace';ctx.fillStyle='#c4c6ca';ctx.strokeStyle='#7d7f8355';ctx.textAlign='right';
  for(let v=0;v<=max;v+=grid){const yy=y(v);ctx.setLineDash([7,6]);ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(width-right,yy);ctx.stroke();ctx.fillText(fmt(v,0),left-7,yy+4);}ctx.setLineDash([]);
  ctx.textAlign='center';for(let i=0;i<=6;i++)ctx.fillText(clock(start+(end-start)*i/6),left+pw*i/6,height-6);
  ctx.strokeStyle='#eeeeec';ctx.lineWidth=1;let last=null;
  for(const q of points){if(q.status==='ok'&&q.latency!=null){if(last&&!q.break_before&&q.ts-last.ts<=(p.interval||1)*(p.graph_step||1)*3+2){ctx.beginPath();ctx.moveTo(x(last.ts),y(last.latency));ctx.lineTo(x(q.ts),y(q.latency));ctx.stroke();}else{ctx.fillStyle='#eee';ctx.fillRect(x(q.ts)-1,y(q.latency)-1,2,2);}last=q;}else{last=null;ctx.fillStyle=q.status==='error'?'#499ddd99':'#f43f4b99';ctx.fillRect(x(q.ts)-1,top,2,ph);}}
  ctx.save();ctx.translate(12,top+ph/2);ctx.rotate(-Math.PI/2);ctx.fillStyle='#ddd';ctx.fillText('RTT · мс',0,0);ctx.restore();ctx.save();ctx.translate(width-9,top+ph/2);ctx.rotate(Math.PI/2);ctx.fillStyle='#ff6b6b';ctx.fillText('Нет ответа: '+fmt(p.stats?.loss,1)+'%',0,0);ctx.restore();
  const base=ctx.getImageData(0,0,canvas.width,canvas.height);
  const summary=`Средняя задержка ${fmt(p.stats?.avg)} мс · максимум ${fmt(p.stats?.max)} мс${peak>max?' · есть значения выше шкалы':''}`;
  const showPoint=e=>{ctx.putImageData(base,0,0);const ts=start+Math.max(0,Math.min(1,(e.clientX-rect.left-left)/pw))*(end-start);const index=points.reduce((best,q,i)=>best<0||Math.abs(q.ts-ts)<Math.abs(points[best].ts-ts)?i:best,-1),q=points[index];if(!q){readout.textContent=summary;return;}const delta=q.delta;readout.textContent=`${clock(q.ts)} · RTT ${q.status==='ok'?fmt(q.latency,2)+' мс':labels[q.status]} · Jitter ${fmt(q.jitter,2)} мс`;;ctx.save();ctx.setLineDash([3,3]);ctx.strokeStyle='#60a5fa';ctx.beginPath();ctx.moveTo(x(q.ts),top);ctx.lineTo(x(q.ts),height-bottom);ctx.stroke();if(q.latency!=null){const yy=y(Math.min(max,q.latency));ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(width-right,yy);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#60a5fa';ctx.beginPath();ctx.arc(x(q.ts),yy,3,0,Math.PI*2);ctx.fill();}ctx.restore();};
  readout.textContent=summary;canvas.onmousemove=showPoint;canvas.onclick=e=>{state.probe=start+Math.max(0,Math.min(1,(e.clientX-rect.left-left)/pw))*(end-start);showPoint(e);};canvas.onmouseleave=()=>{ctx.putImageData(base,0,0);readout.textContent=summary;if(state.probe>=start&&state.probe<=end)showPoint({clientX:rect.left+x(state.probe)});};if(state.probe>=start&&state.probe<=end)showPoint({clientX:rect.left+x(state.probe)});
  canvas.onwheel=e=>{e.preventDefault();state.window=Math.max(60,Math.min(86400,Math.round(state.window*(e.deltaY>0?1.5:1/1.5))));let option=[...$('window').options].find(o=>Number(o.value)===state.window);if(!option){option=new Option(Math.round(state.window/60)+' мин',state.window);$('window').add(option);}$('window').value=state.window;clearTimeout(state.zoomTimer);state.zoomTimer=setTimeout(refresh,150);};
  canvas.onpointerdown=e=>{state.dragStart=Math.max(start,Math.min(end,start+(e.clientX-rect.left-left)/pw*(end-start)));if(e.pointerId!=null)canvas.setPointerCapture(e.pointerId);};canvas.onpointerup=e=>{const down=state.dragStart;if(down==null)return;const t=Math.max(start,Math.min(end,start+(e.clientX-rect.left-left)/pw*(end-start)));if(Math.abs(t-down)>=5){state.focus=Math.max(t,down);state.window=Math.max(60,Math.min(86400,Math.round(Math.abs(t-down))));const d=new Date(state.focus*1000);$('focus-end').value=new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,19);let option=[...$('window').options].find(o=>Number(o.value)===state.window);if(!option){option=new Option(Math.round(state.window/60)+' мин',state.window);$('window').add(option);}$('window').value=state.window;refresh();}state.dragStart=null;};canvas.onmousedown=canvas.onpointerdown;canvas.onmouseup=canvas.onpointerup;
  const jc=$('jitter-chart');jc.width=width*ratio;jc.height=48*ratio;const j=jc.getContext('2d');j.scale(ratio,ratio);j.fillStyle='#141d2b';j.fillRect(left,0,pw,46);
  const ds=points.map(q=>({ts:q.ts,v:q.jitter}));
  const jm=Math.max(50,nice(Math.max(1,...ds.map(d=>d.v||0))));
  const jy=v=>44-v/jm*38;
  j.strokeStyle='#ff744e';j.lineWidth=1;j.beginPath();let connected=false;
  ds.forEach((d,i)=>{if(d.v==null){connected=false;return;}const xx=x(d.ts),yy=jy(d.v);if(connected&&d.ts-ds[i-1].ts<=(p.interval||1)*(p.graph_step||1)*3+2)j.lineTo(xx,yy);else j.moveTo(xx,yy);connected=true;});j.stroke();
  j.strokeStyle='#ffffff55';j.setLineDash([6,5]);j.beginPath();j.moveTo(left,jy(35));j.lineTo(width-right,jy(35));j.stroke();j.setLineDash([]);
  j.fillStyle='#ff744e';j.font='10px system-ui';j.fillText('Jitter · сглаживание 1/16 · ориентир 35 мс',left+8,11);j.fillText(fmt(jm,0)+' мс',3,10);j.fillText('35',18,jy(35)-2);j.fillText('0',25,45);jc.onmousemove=showPoint;jc.onmouseleave=canvas.onmouseleave;

}
