// One day in detail: every test, the day's statistics, its DNA, its probes.
import { api, qs, state, h, clear, section, n, pct, fmtTime, fmtDay, css, BASE } from '../util.js';
import { TimeChart, speedPane, latencyPane, speedTooltip, latencyTooltip } from '../timechart.js';
import { dnaGlyph, Timeline } from '../viz.js';
import { openTest } from './detail.js';

export async function mount(root, { day }) {
  const st = state.settings;
  const page = h('div', { class: 'page' });
  root.append(page);
  const cleanups = [];
  const q = qs({ range: 'custom', from: day, to: day });
  const sm = await api('/summary?' + q);
  const a = sm.range.from_ms, b = sm.range.to_ms;
  const prev = new Date(Date.parse(day + 'T12:00:00Z') - 86400e3).toISOString().slice(0, 10);
  const next = new Date(Date.parse(day + 'T12:00:00Z') + 86400e3).toISOString().slice(0, 10);
  const s = sm.summary;
  page.append(h('div', { style: { padding: '28px 0 8px', display: 'flex', alignItems: 'baseline', gap: '16px', flexWrap: 'wrap' } },
    h('h1', { style: { fontSize: '40px' }, text: fmtDay(Date.parse(day + 'T12:00:00Z')) }),
    h('span', { class: 'muted', text: `${n(s.n)} scheduled tests · ${n(s.n_all)} in total · median ${n(s.download.median, 0)} Mbps · ${pct(s.pct_target, 0)} on target` }),
    h('span', { style: { marginLeft: 'auto', display: 'flex', gap: '8px' } }, h('a', { class: 'btn small', href: `${BASE}/day/${prev}`, text: '← previous day' }),
      h('a', { class: 'btn small', href: `${BASE}/day/${next}`, text: 'next day →' }))));
  const chartBox = h('div', { class: 'chart' });
  const chart = new TimeChart(chartBox, { panes: [speedPane('dl', 'Download (Mbps)', '--dl', st, 220), speedPane('ul', 'Upload (Mbps)', '--ul', st, 100)],
    fetch: (x, y, px) => api('/series?' + qs({ from: x, to: y, px })), tooltip: speedTooltip(st), onPick: (p) => openTest(p[5]), range: [a, b] });
  const latBox = h('div', { class: 'chart' });
  const lat = new TimeChart(latBox, { panes: [latencyPane(st, 120)], fetch: (x, y, px) => api('/latency?' + qs({ from: x, to: y, px })), tooltip: latencyTooltip(), range: [a, b] });
  const tlBox = h('div', { class: 'timeline' });
  const tl = new Timeline(tlBox);
  cleanups.push(() => chart.destroy(), () => lat.destroy(), () => tl.destroy());
  page.append(section('01', 'Tests', 'click a point for its full record', chartBox), section('02', 'Latency & reliability', '', latBox, h('div', { style: { height: '14px' } }), tlBox));
  chart.setRange(a, b); lat.setRange(a, b);
  tl.set(await api('/timeline?' + q));
  const dna = await api('/dna?' + q);
  const cv = h('canvas');
  const tbl = h('table', { class: 'data' }, h('thead', {}, h('tr', {}, ...['Hour', 'Tests', 'Median', 'P10', 'Ping', 'Failed'].map(t => h('th', { text: t })))),
    h('tbody', {}, ...dna.spokes.filter(x => x.n).map(x => h('tr', {}, h('td', { text: `${String(x.hour).padStart(2, '0')}:00` }), h('td', { text: n(x.n) }),
      h('td', { text: n(x.median, 0) }), h('td', { text: n(x.p10, 0) }), h('td', { text: n(x.ping, 1) }), h('td', { text: n(x.failed) })))));
  page.append(section('03', 'Connection DNA', 'this day as a fingerprint', h('div', { class: 'grid g-5-7' }, h('div', { class: 'dna' }, cv), h('div', { class: 'table-wrap' }, tbl))));
  requestAnimationFrame(() => dnaGlyph(cv, dna, st, 300));
  return { destroy() { cleanups.forEach(f => f()); } };
}
