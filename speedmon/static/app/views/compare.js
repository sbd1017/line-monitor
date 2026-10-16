// Compare two periods: this week vs last week, month vs month, before/after a date, or any custom pair.
import { api, qs, state, h, clear, section, seg, n, pct, css, localParts } from '../util.js';
import { lineChart, histogram } from '../viz.js';

function iso(d) { return d.toISOString().slice(0, 10); }
function presets() {
  const now = new Date(); const p = localParts(Date.now());
  const today = new Date(Date.UTC(p.y, p.m - 1, p.d));
  const dow = (today.getUTCDay() + 6) % 7;
  const monThis = new Date(today - dow * 86400e3);
  const monLast = new Date(monThis - 7 * 86400e3);
  const firstThis = new Date(Date.UTC(p.y, p.m - 1, 1)), firstLast = new Date(Date.UTC(p.y, p.m - 2, 1));
  const lastOfLast = new Date(firstThis - 86400e3), lastOfPrev = new Date(firstLast - 86400e3), firstPrev = new Date(Date.UTC(p.y, p.m - 3, 1));
  return {
    week: [[iso(monLast), iso(new Date(monThis - 86400e3))], [iso(monThis), iso(today)], 'Last week vs this week'],
    month: [[iso(firstPrev), iso(lastOfPrev)], [iso(firstLast), iso(lastOfLast)], 'Month before last vs last month'],
    thismonth: [[iso(firstLast), iso(lastOfLast)], [iso(firstThis), iso(today)], 'Last month vs this month'],
  };
}

export async function mount(root) {
  const st = state.settings;
  const page = h('div', { class: 'page' });
  root.append(page);
  const P = presets();
  const ins = { af: h('input', { type: 'date' }), at: h('input', { type: 'date' }), bf: h('input', { type: 'date' }), bt: h('input', { type: 'date' }) };
  const pivot = h('input', { type: 'date' });
  const setPair = ([a, b]) => { ins.af.value = a[0]; ins.at.value = a[1]; ins.bf.value = b[0]; ins.bt.value = b[1]; load(); };
  const out = h('div', {});
  page.append(h('div', { class: 'filters' },
    seg([['week', 'Week vs week'], ['thismonth', 'Month vs month'], ['month', 'Previous months']], 'week', (v) => setPair(P[v]), 'Preset'),
    h('span', { class: 'lod', text: 'A' }), ins.af, ins.at, h('span', { class: 'lod', text: 'B' }), ins.bf, ins.bt,
    h('button', { class: 'btn small', text: 'Compare', onclick: load }),
    h('span', { class: 'lod', style: { marginLeft: '12px' }, text: 'Before/after' }), pivot,
    h('button', { class: 'btn small', text: '30 days either side', onclick: () => {
      if (!pivot.value) return; const d = new Date(pivot.value + 'T12:00:00Z');
      setPair([[iso(new Date(d - 30 * 86400e3)), iso(new Date(d - 86400e3))], [pivot.value, iso(new Date(+d + 29 * 86400e3))]]);
    } })), out);

  async function load() {
    if (!(ins.af.value && ins.at.value && ins.bf.value && ins.bt.value)) return;
    out.style.opacity = .5;
    const r = await api('/compare?' + qs({ a_range: 'custom', a_from: ins.af.value, a_to: ins.at.value, b_range: 'custom', b_from: ins.bf.value, b_to: ins.bt.value }));
    out.style.opacity = 1;
    render(r);
  }

  function render(r) {
    clear(out);
    const A = r.a, B = r.b;
    const row = (label, fa, fb, fmt, better = 'up') => {
      const va = fa(A), vb = fb(B);
      const diff = (va != null && vb != null) ? vb - va : null;
      const good = diff == null || better === 'none' ? null : (better === 'up' ? diff > 0 : diff < 0);
      return h('tr', {}, h('td', { text: label }), h('td', { class: 'r', text: fmt(va) }), h('td', { class: 'r', text: fmt(vb) }),
        h('td', { class: 'r', style: { color: good == null || Math.abs(diff) < 1e-9 ? '' : good ? css('--good') : css('--crit') }, text: diff == null ? '—' : (diff > 0 ? '+' : '') + fmt(diff) }));
    };
    const m = (v) => v == null ? '—' : n(v, 1);
    const p = (v) => v == null ? '—' : pct(v);
    const table = h('table', { class: 'data' }, h('thead', {}, h('tr', {}, h('th', { text: '' }), h('th', { class: 'r', text: `A · ${r.a_range.label}` }), h('th', { class: 'r', text: `B · ${r.b_range.label}` }), h('th', { class: 'r', text: 'B − A' }))),
      h('tbody', {},
        row('Scheduled tests', x => x.n, x => x.n, v => v == null ? '—' : n(v), 'none'),
        row('Median download (Mbps)', x => x.download.median, x => x.download.median, m),
        row('Mean download (Mbps)', x => x.download.mean, x => x.download.mean, m),
        row('P10 download (Mbps)', x => x.download.p['10'], x => x.download.p['10'], m),
        row('On target (%)', x => x.pct_target, x => x.pct_target, p),
        row('Degraded (%)', x => x.pct_degraded, x => x.pct_degraded, p, 'down'),
        row('Severe (%)', x => x.pct_severe, x => x.pct_severe, p, 'down'),
        row('Failed, ISP side (%)', x => x.pct_failed, x => x.pct_failed, p, 'down'),
        row('Median upload (Mbps)', x => x.upload.median, x => x.upload.median, m),
        row('Median ping (ms)', x => x.ping.median, x => x.ping.median, m, 'down'),
        row('Median jitter (ms)', x => x.jitter.median, x => x.jitter.median, (v) => v == null ? '—' : n(v, 2), 'down')));
    const t = r.download_test;
    out.append(section('01', 'Verdict', '', h('div', { class: 'finding' + (t.p != null && t.p < 0.05 ? '' : ' none') }, h('div', { class: 'h', text: r.verdict }),
      h('div', { class: 'd', text: `Hodges–Lehmann shift (B − A): ${r.shift_mbps == null ? '—' : n(r.shift_mbps, 1) + ' Mbps'} · Mann–Whitney p ${t.p == null ? '—' : t.p < 1e-6 ? '< 0.000001' : n(t.p, 4)} · uses scheduled tests only.` })),
      h('div', { class: 'table-wrap', style: { marginTop: '18px' } }, table)));
    const hourBox = h('div', {});
    out.append(section('02', 'Hour-of-day profile', 'median download by local hour', h('div', { class: 'legend' }, h('span', {}, h('i', { style: { background: css('--dl') } }), 'A'), h('span', {}, h('i', { style: { background: css('--ul') } }), 'B')), hourBox));
    lineChart(hourBox, [{ name: 'A', color: '--dl', points: r.hourly_a.map(x => [x.hour, x.median]) }, { name: 'B', color: '--ul', points: r.hourly_b.map(x => [x.hour, x.median]) }],
      { target: st.target_down, xFmt: (x) => String(x).padStart(2, '0') + ':00' });
    const ha = h('div', {}), hb = h('div', {});
    out.append(section('03', 'Distributions', 'share of scheduled tests per 25 Mbps band', h('div', { class: 'grid g2' }, h('div', { class: 'block' }, h('h3', { text: 'A' }), ha), h('div', { class: 'block' }, h('h3', { text: 'B' }), hb))));
    histogram(ha, r.hist_a, st);
    histogram(hb, r.hist_b, st, { color: '--ul' });
  }
  setPair(P.week);
  return {};
}
