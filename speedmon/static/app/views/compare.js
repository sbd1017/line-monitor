// Compare two periods: this week vs last week, month vs month, before/after a date, or any custom pair.
import { api, qs, state, h, clear, section, seg, n, pct, css, localParts } from '../util.js';
import { lineChart, histogram } from '../viz.js';

function iso(d) { return d.toISOString().slice(0, 10); }
const DAY = 86400e3;
function today() { const p = localParts(Date.now()); return new Date(Date.UTC(p.y, p.m - 1, p.d)); }
function presets() {
  const p = localParts(Date.now()), t = today();
  const monThis = new Date(t - ((t.getUTCDay() + 6) % 7) * DAY), monLast = new Date(monThis - 7 * DAY);
  const firstThis = new Date(Date.UTC(p.y, p.m - 1, 1)), firstLast = new Date(Date.UTC(p.y, p.m - 2, 1));
  return {
    week: [[iso(monLast), iso(new Date(monThis - DAY))], [iso(monThis), iso(t)]],
    month: [[iso(firstLast), iso(new Date(firstThis - DAY))], [iso(firstThis), iso(t)]],
  };
}
const MODES = [['week', 'This week vs last week'], ['month', 'This month vs last month'], ['change', 'Before / after a date'], ['custom', 'Custom']];
const nice = (s) => s === iso(today()) ? 'today' : new Date(s + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });

export async function mount(root) {
  const st = state.settings;
  const page = h('div', { class: 'page' });
  root.append(page);
  const P = presets();
  const ins = { af: h('input', { type: 'date' }), at: h('input', { type: 'date' }), bf: h('input', { type: 'date' }), bt: h('input', { type: 'date' }) };
  const pivot = h('input', { type: 'date', max: iso(today()) });
  let mode = 'week', span = 14;
  const pair = () => [[ins.af.value, ins.at.value], [ins.bf.value, ins.bt.value]];
  const setPair = ([a, b]) => { ins.af.value = a[0]; ins.at.value = a[1]; ins.bf.value = b[0]; ins.bt.value = b[1]; load(); };
  const fromPivot = () => {
    if (!pivot.value) { clear(out); summary.textContent = 'Pick the day something changed (a new router, a call to Virgin, an upgrade) to compare the days before it with the days after.'; return; }
    const d = new Date(pivot.value + 'T00:00:00Z'), end = Math.min(+d + (span - 1) * DAY, +today());
    setPair([[iso(new Date(d - span * DAY)), iso(new Date(d - DAY))], [pivot.value, iso(new Date(end))]]);
  };
  pivot.onchange = fromPivot;
  const changeBox = h('div', { class: 'cmp-opts' }, h('label', { class: 'field inline' }, 'Changed on', pivot),
    seg([[7, '7 days'], [14, '14 days'], [30, '30 days']], span, (v) => { span = +v; fromPivot(); }, 'Days either side'), h('span', { class: 'muted', text: 'either side' }));
  const customBox = h('div', { class: 'cmp-opts' },
    h('label', { class: 'field inline' }, 'A from', ins.af, 'to', ins.at), h('label', { class: 'field inline' }, 'B from', ins.bf, 'to', ins.bt),
    h('button', { class: 'btn small primary', text: 'Compare', onclick: load }));
  const summary = h('p', { class: 'cmp-summary' });
  const out = h('div', {});
  const show = () => { changeBox.hidden = mode !== 'change'; customBox.hidden = mode !== 'custom'; };
  page.append(h('div', { class: 'filters cmp-bar' },
    seg(MODES, mode, (v) => { mode = v; show(); if (P[v]) setPair(P[v]); else if (v === 'change') fromPivot(); }, 'What to compare'),
    changeBox, customBox), summary, out);
  show();

  async function load() {
    const [a, b] = pair();
    if (!(a[0] && a[1] && b[0] && b[1])) return;
    summary.textContent = '';
    summary.append(h('b', { text: 'A' }), ` ${nice(a[0])} to ${nice(a[1])}`, h('span', { class: 'muted', text: '  compared with  ' }), h('b', { text: 'B' }), ` ${nice(b[0])} to ${nice(b[1])}`);
    out.style.opacity = .5;
    const r = await api('/compare?' + qs({ a_range: 'custom', a_from: a[0], a_to: a[1], b_range: 'custom', b_from: b[0], b_to: b[1] }));
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
    const table = h('table', { class: 'data' }, h('thead', {}, h('tr', {}, h('th', { text: '' }), h('th', { class: 'r', text: 'A' }), h('th', { class: 'r', text: 'B' }), h('th', { class: 'r', text: 'B − A' }))),
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
    const sig = t.p != null && t.p < 0.05;
    let detail;
    if (!r.enough) {
      const before = r.data_from_ms && r.a_range.from_ms < r.data_from_ms;
      detail = before ? `Records start on ${new Date(r.data_from_ms).toLocaleDateString('en-GB', { day: 'numeric', month: 'long' })}, so period A is mostly or entirely before them. Pick dates after that, or check back when each period has a few days of tests.`
        : 'Each period needs at least 10 successful scheduled tests. Check back later, or pick longer periods.';
    } else {
      detail = `Typical difference, B against A: ${r.shift_mbps == null ? '—' : (r.shift_mbps > 0 ? '+' : '') + n(r.shift_mbps, 1) + ' Mbps'} · ${sig ? 'a real difference, not just day-to-day variation' : 'within normal day-to-day variation'} · scheduled tests only`;
    }
    out.append(section('01', 'Verdict', '', h('div', { class: 'finding' + (r.enough && sig ? '' : ' none') }, h('div', { class: 'h', text: r.verdict }),
      h('div', { class: 'd', text: detail, title: r.enough ? `Hodges–Lehmann shift; Mann–Whitney p ${t.p == null ? '—' : t.p < 1e-6 ? '< 0.000001' : n(t.p, 4)}` : null })),
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
