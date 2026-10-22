'use strict';

// Pure SVG renderer for the measured route.
function routeGraph(hops, destination, latencyLimit = 100) {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  if (!Array.isArray(hops) || !hops.length) return '';
  const rows = hops.map(h => ({
    hop: Number.isFinite(Number(h.hop)) ? Number(h.hop) : '?',
    ip: h.ip || null,
    latency: typeof h.latency === 'number' && Number.isFinite(h.latency) && h.latency >= 0 ? h.latency : null,
  }));
  const largest = Math.max(1, ...rows.map(h => h.latency ?? 0));
  const scale = Math.pow(10, Math.floor(Math.log10(largest)));
  const maximum = Math.ceil(largest * 1.15 / scale) * scale;
  const width = 720, height = rows.length * 40 + 72, x0 = 310, plotWidth = 315;
  const number = v => v.toLocaleString('ru-RU', {maximumFractionDigits:2});
  let content = `<title>Traceroute: задержка до каждого узла</title><desc>Узлы расположены по порядку маршрута. Длина полосы показывает RTT до узла. Отсутствие ответа показано отдельно и не равно нулевой задержке.</desc><rect width="${width}" height="${height}" fill="#121c29"/><g font-family="ui-monospace,Consolas,monospace" font-size="11" fill="#93a5ba"><text x="15" y="19">УЗЕЛ</text><text x="91" y="19">IP-АДРЕС</text><text x="${x0}" y="19">RTT ДО УЗЛА, мс</text><text x="695" y="19" text-anchor="end">ОТВЕТ</text>`;
  for (let i = 0; i <= 4; i++) {
    const x = x0 + plotWidth * i / 4;
    content += `<line x1="${x}" x2="${x}" y1="32" y2="${height-25}" stroke="#263548"/><text x="${x}" y="${height-7}" text-anchor="middle">${number(maximum*i/4)}</text>`;
  }
  if (latencyLimit > 0 && latencyLimit <= maximum) {
    const x = x0 + plotWidth * latencyLimit / maximum;
    content += `<line x1="${x}" x2="${x}" y1="32" y2="${height-25}" stroke="#f4c677" stroke-dasharray="4 4"><title>Порог задержки ${esc(latencyLimit)} мс</title></line>`;
  }
  content += '</g>';
  rows.forEach((h, i) => {
    const y = 52 + i * 40, answered = h.latency !== null;
    const reached = Boolean(destination && h.ip === destination);
    const color = !answered ? '#93a5ba' : h.latency > latencyLimit ? '#f4c677' : reached ? '#6aa9ff' : '#71e2bd';
    if (i) content += `<line x1="61" x2="61" y1="${y-31}" y2="${y-9}" stroke="#415268" ${!h.ip || !rows[i-1].ip ? 'stroke-dasharray="3 3"' : ''}/>`;
    content += `<g font-family="ui-monospace,Consolas,monospace" font-size="12"><title>Узел ${esc(h.hop)}: ${esc(h.ip||'IP не определён')}; ${answered?number(h.latency)+' мс':'нет измеренного RTT'}${reached?'; конечная цель':''}</title><text x="20" y="${y+4}" fill="#93a5ba">${esc(h.hop)}</text><circle cx="61" cy="${y}" r="6" fill="${answered?color:'#121c29'}" stroke="${color}" stroke-width="2"/><text x="91" y="${y+4}" fill="${h.ip?'#ecf2f9':'#93a5ba'}">${esc(h.ip||'Нет ответа')}</text>`;
    if (answered) {
      const length = Math.max(2, plotWidth * h.latency / maximum);
      content += `<rect x="${x0}" y="${y-7}" width="${length}" height="14" rx="3" fill="${color}" opacity=".8"/><text x="695" y="${y+4}" text-anchor="end" fill="${color}">${number(h.latency)} мс</text>`;
    } else {
      content += `<text x="${x0}" y="${y+4}" fill="#93a5ba" font-size="11">RTT не измерен</text><text x="695" y="${y+4}" text-anchor="end" fill="#93a5ba">—</text>`;
    }
    content += '</g>';
  });
  return `<svg class="traceroute-svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="График traceroute: задержка до ${rows.length} узлов">${content}</svg>`;
}
