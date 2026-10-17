// Evidence: a clean, objective report of a period, readable by a third party without the dashboard. Prints to A4.
import { api, qs, state, h, clear, section, seg, n, pct, fmtDT, fmtDate, dur, toast } from '../util.js';
import { columns, Heatmap, lineChart } from '../viz.js';

const PRESETS = [['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days'], ['this_month', 'This month'], ['last_month', 'Last month'], ['this_year', 'This year'], ['all', 'All']];

export async function mount(root, params) {
  const st = state.settings;
  const page = h('div', { class: 'page' });
  root.append(page);
  let preset = params.range || '30d', custom = null;
  const fromIn = h('input', { type: 'date' }), toIn = h('input', { type: 'date' });
  const bar = h('div', { class: 'filters evidence-bar' }, h('span', { class: 'lod', text: 'Period' }),
    seg(PRESETS, preset, (v) => { preset = v; custom = null; load(); }, 'Period'), fromIn, toIn,
    h('button', { class: 'btn small', text: 'Apply', onclick: () => { if (fromIn.value && toIn.value) { custom = [fromIn.value, toIn.value]; load(); } } }),
    h('span', { style: { marginLeft: 'auto', display: 'flex', gap: '8px' } },
      h('button', { class: 'btn', text: 'Print / save as PDF', onclick: () => window.print() }),
      h('button', { class: 'btn primary', text: 'Download XLSX of this period', onclick: () => exportXlsx() })));
  const paper = h('article', { class: 'paper' });
  const integ = h('div', { class: 'evidence-integrity', id: 'integrity' });
  page.append(bar, paper, integ);
  const cleanups = [];
  const { mountInto } = await import('./integrity.js');
  integ.append(section('', 'Integrity', 'proof that the data in this report hasn\'t been changed, and that nothing went missing'));
  const live = await mountInto(integ);
  cleanups.push(() => live?.destroy?.());
  if (params.focus === 'integrity') setTimeout(() => integ.scrollIntoView({ block: 'start' }), 300);

  function rq() { return custom ? { range: 'custom', from: custom[0], to: custom[1] } : { range: preset }; }
  async function exportXlsx() {
    try {
      const body = custom ? { format: 'xlsx', range: 'custom', from: custom[0], to: custom[1] } : { format: 'xlsx', range: preset };
      await api('/exports', { body });
      toast('Export started. It appears on the Export page when ready.');
      window.__go('/export');
    } catch (e) { toast(e.message); }
  }

  async function load() {
    paper.style.opacity = .5;
    const r = await api('/evidence?' + qs(rq()));
    paper.style.opacity = 1;
    render(r);
  }

  function render(r) {
    clear(paper);
    cleanups.splice(0).forEach(f => f());
    const sm = r.summary, dl = sm.download, ul = sm.upload, s = r.settings, ta = r.time, cov = r.coverage, lat = r.latency;
    const T = (rows, head) => h('table', { class: 'facts' }, head ? h('thead', {}, h('tr', {}, ...head.map((x, i) => h('th', { class: i ? 'r' : '', text: x })))) : null,
      h('tbody', {}, ...rows.filter(Boolean).map(row => h('tr', {}, ...row.map((c, i) => h(i ? 'td' : 'th', { class: i ? 'r' : '', text: c ?? '—' }))))));
    const sec = (title, ...kids) => h('section', {}, h('h2', { text: title }), ...kids);
    paper.append(
      h('div', { style: { fontSize: '11px', letterSpacing: '.14em', textTransform: 'uppercase', color: 'var(--dl)', fontWeight: 700 }, text: 'Broadband performance evidence' }),
      h('h1', { text: s.package_name }),
      h('p', { class: 'meta', text: `Period: ${r.range.from_local} to ${r.range.to_local} (${r.range.label}). Generated ${r.generated_local} by ${r.software}.` }),
      h('p', { class: 'meta', text: `Measured by a Raspberry Pi 5 wired by Ethernet to the home router, which is wired to the Virgin Media Hub in modem mode. Full speed tests with the Ookla Speedtest CLI every ${s.interval_min} minutes at a random moment in each slot, light connectivity probes every minute.` }));
    const cap = r.capacity;
    if (cap && !['ok', 'unknown'].includes(cap.level)) paper.append(h('p', { class: 'cap-note' + (cap.level === 'advertised' ? '' : ' bad'), text: cap.text }));
    paper.append(sec('Summary',
      h('div', { class: 'answers' },
        ...[['Typical download (median)', `${n(dl.median, 0)} Mbps`, `${pct(sm.pct_of_advertised_median, 0)} of the advertised ${s.advertised_down} Mbps`],
          ['Achieved the target', pct(sm.pct_target), `of scheduled tests reached ${s.target_down} Mbps`],
          ['Consistently observed (P10)', dl.p['10'] == null ? '—' : `${n(dl.p['10'], 0)} Mbps`, '90% of tests were at least this fast'],
          ['Highest measured', `${n(dl.max, 0)} Mbps`, ''], ['Lowest successful', `${n(dl.min, 0)} Mbps`, ''],
          ['Degraded results', pct(sm.pct_degraded), `below ${s.degraded_down} Mbps`], ['Severely degraded', pct(sm.pct_severe), `below ${s.severe_down} Mbps`],
          ['Failed tests (ISP side)', pct(sm.pct_failed), `${n(sm.n_failed_isp)} tests; outages ${n(sm.n_outage)}`],
          ['Upload (median)', `${n(ul.median, 1)} Mbps`, `advertised ${s.advertised_up} Mbps`]]
          .map(([k, v, x]) => h('div', {}, h('div', { class: 'k', text: k }), h('div', { class: 'v', text: v }), h('div', { class: 's', text: x })))),
    ));
    paper.append(sec('Measurement period',
      T([['Package', `${s.package_name}: ${s.advertised_down} Mbps down / ${s.advertised_up} Mbps up advertised`],
        s.minimum_guaranteed_down ? ['Minimum guaranteed download', `${s.minimum_guaranteed_down} Mbps`] : null,
        ['Thresholds used', `on target ≥ ${s.target_down} · degraded < ${s.degraded_down} · severe < ${s.severe_down} Mbps (download)`],
        ['First / last measurement', `${fmtDT(sm.first_ms)} / ${fmtDT(sm.last_ms)}`],
        ['Scheduled tests (statistical basis)', n(sm.n)], ['All tests incl. confirmation/burst/manual', n(sm.n_all)],
        ['Successful / failed (ISP side) / Pi-LAN side', `${n(sm.n_ok)} / ${n(sm.n_failed_isp)} / ${n(sm.n_local)}`],
        ['Excluded by the owner (with reasons)', n(sm.excluded)],
        ['Monitoring coverage', `${pct(cov.pct)} of the period monitored · ${n(cov.tests_scheduled)} of ~${n(cov.tests_expected)} scheduled tests`],
        r.legacy_in_range ? ['Earlier speedtest-cli data in this period', `${n(r.legacy_in_range)} rows, ${s.include_legacy ? 'included' : 'not included'} (different method; see methodology)`] : null])));
    paper.append(sec('Download and upload statistics',
      T([['Mean', n(dl.mean, 1), n(ul.mean, 1)], ['Median', n(dl.median, 1), n(ul.median, 1)], ['Minimum', n(dl.min, 1), n(ul.min, 1)], ['Maximum', n(dl.max, 1), n(ul.max, 1)],
        ...['1', '5', '10', '90', '95', '99'].map(p => [`P${p}`, n(dl.p[p], 1), n(ul.p[p], 1)]), ['Standard deviation', n(dl.sd, 1), n(ul.sd, 1)],
        ['Successful tests', n(dl.n), n(ul.n)]], ['Mbps', 'Download', 'Upload']),
      h('p', { class: 'meta', text: 'Percentiles are shown only where the sample is large enough (P1/P99 need 100 tests, P5/P95 20, P10/P90 10). "—" means too few tests.' }),
      T([...sm.tiers.map(t => [`Tests at or above ${t.mbps} Mbps`, pct(t.pct)]), [`Below the ${s.target_down} Mbps target`, pct(sm.pct_below_target)],
        [`Below ${s.degraded_down} Mbps (degraded)`, pct(sm.pct_degraded)], [`Below ${s.severe_down} Mbps (severe)`, pct(sm.pct_severe)],
        sm.pct_below_guarantee != null ? ['Below the minimum guarantee', pct(sm.pct_below_guarantee)] : null,
        [`Upload at or above ${s.target_up} Mbps`, pct(sm.up_pct_target)]], ['Share of successful scheduled tests', ''])));
    const qs_ = sm.quality;
    if (qs_ && qs_.n_ok) {
      const cq = qs_.clean, flagged = Object.entries(qs_.flags).sort((a, b) => b[1] - a[1]);
      paper.append(sec('Measurement quality',
        h('p', { class: 'meta', text: `${n(qs_.n_clean)} of ${n(qs_.n_ok)} successful scheduled tests were clean: nothing on the monitor's side could have held them down. The figures below set the others aside, to show they don't change the picture.` }),
        T([['On target', pct(sm.pct_target), pct(cq.pct_target)], [`Degraded (below ${s.degraded_down} Mbps)`, pct(sm.pct_degraded), pct(cq.pct_degraded)],
          [`Severe (below ${s.severe_down} Mbps)`, pct(sm.pct_severe), pct(cq.pct_severe)],
          sm.pct_below_guarantee != null ? ['Below the minimum guarantee', pct(sm.pct_below_guarantee), pct(cq.pct_below_guarantee)] : null,
          ['Median download (Mbps)', n(dl.median, 1), n(cq.download.median, 1)], ['Tests', n(qs_.n_ok), n(qs_.n_clean)]],
          ['', 'All scheduled tests', 'Clean tests only']),
        flagged.length ? T(flagged.map(([c, k]) => [qs_.labels[c], n(k)]), ['Label', 'Tests']) : null,
        h('p', { class: 'meta', text: 'Labels come from the Pi\'s own diagnostics recorded with each test (see Methodology). Traffic from other devices in the home is not part of this check.' })));
    }
    const dailyBox = h('div', {});
    paper.append(sec('Results over the period', dailyBox, h('p', { class: 'meta', text: 'Daily median and P10 (the speed 90% of that day\'s tests exceeded) of scheduled tests.' })));
    lineChart(dailyBox, [{ name: 'Median download', color: '--dl', points: r.daily.map(d => [d.day, d.median]) },
      { name: 'P10 download', color: '--dl', dash: '4 3', width: 1.5, opacity: .8, points: r.daily.map(d => [d.day, d.p10]) },
      { name: 'Median upload', color: '--ul', points: r.daily.map(d => [d.day, d.ul_median]) }], { target: s.target_down, xFmt: (d) => d.slice(5) });
    const hourBox = h('div', {});
    const heatBox = h('div', { class: 'heat' });
    paper.append(sec('When performance is worst', hourBox,
      h('p', { class: 'meta', text: 'Median download by local hour of day; the tick is the P10.' }), heatBox,
      T([['Worst hour of day', ta.hours.worst ? `${String(ta.hours.worst.hour).padStart(2, '0')}:00–${String(ta.hours.worst.hour).padStart(2, '0')}:59 · median ${n(ta.hours.worst.median, 0)} Mbps` : 'not enough data'],
        ['Best hour of day', ta.hours.best ? `${String(ta.hours.best.hour).padStart(2, '0')}:00 · median ${n(ta.hours.best.median, 0)} Mbps` : '—'],
        ['Worst day', ta.days.worst[0] ? `${ta.days.worst[0].day} · median ${n(ta.days.worst[0].median, 0)} Mbps (${ta.days.worst[0].n_ok} tests)` : `needs ≥ ${ta.days.min_samples} tests a day`],
        ['Best day', ta.days.best[0] ? `${ta.days.best[0].day} · median ${n(ta.days.best[0].median, 0)} Mbps` : '—'],
        ['Worst day of week', ta.weekdays.worst ? `${ta.weekdays.worst.name} · median ${n(ta.weekdays.worst.median, 0)} Mbps` : '—'],
        ['Worst 24-hour period (rolling mean)', ta.rolling_24h.worst ? `${fmtDT(ta.rolling_24h.worst.start_ms)} → ${fmtDT(ta.rolling_24h.worst.end_ms)} · ${n(ta.rolling_24h.worst.mean, 0)} Mbps (${ta.rolling_24h.worst.n} tests)` : '—'],
        ['Worst 7-day period (rolling mean)', ta.rolling_7d.worst ? `${fmtDate(ta.rolling_7d.worst.start_ms)} → ${fmtDate(ta.rolling_7d.worst.end_ms)} · ${n(ta.rolling_7d.worst.mean, 0)} Mbps` : '—']]),
      ...(r.congestion.findings.length ? r.congestion.findings.map(f => h('p', { style: { margin: '12px 0', fontWeight: 600 }, text: f.text })) : [h('p', { class: 'meta', text: r.congestion.message || '' })])));
    columns(hourBox, r.hourly, { labelOf: (x) => String(x.hour).padStart(2, '0'), st: s, highlightWorst: 0, height: 200 });
    const hm = new Heatmap(heatBox, {});
    cleanups.push(() => hm.destroy());
    api('/heatmap?' + qs(rq())).then(d => hm.set(d.rows, s));
    const st_ = sm.streaks;
    paper.append(sec('Degradation and outages',
      T([['Degradation incidents (≥ 2 consecutive poor tests)', n(r.degradations.length)],
        ['Longest run of degraded or failed tests', st_.longest_poor ? `${st_.longest_poor.count} tests, ${fmtDT(st_.longest_poor.first_ms)} → ${fmtDT(st_.longest_poor.last_ms)} (sampled span ${dur(st_.longest_poor.sampled_span_ms)})` : 'none'],
        ['Longest run meeting the target', st_.longest_good ? `${st_.longest_good.count} tests (sampled span ${dur(st_.longest_good.sampled_span_ms)})` : 'none'],
        ['Isolated poor results vs runs', `${n(r.answers.isolated_poor.isolated)} isolated · ${n(r.answers.isolated_poor.in_runs)} in runs`],
        ['Confirmed Internet outages (probes)', n(r.outages.length)]]),
      r.degradations.length ? T(r.degradations.slice(-25).reverse().map(d => [fmtDT(d.first_ms), `${d.count} tests`, `${n(d.min_dl, 0)} Mbps`, dur(d.sampled_span_ms)]), ['Degradation started', 'Tests', 'Lowest', 'Sampled span']) : null,
      r.outages.length ? T(r.outages.slice(-25).reverse().map(o => [fmtDT(o.first_ms), `${dur(o.min_duration_ms)} – ${dur(o.max_duration_ms)}`, `${o.probes}`]), ['Outage started', 'Duration (bounds)', 'Failing probes']) : null,
      h('p', { class: 'meta', text: 'Durations between tests are sampled spans, not proof of continuous speed. Outage durations are bounded by the probes either side.' })));
    const g = r.guarantee;
    if (g && g.applies) {
      paper.append(sec(`Minimum guaranteed download speed (${g.mgds} Mbps)`,
        T([['Days with results in this period', n(g.days_with_data)], ['Days with at least one result below the guarantee', n(g.days_below)],
          ['Results below the guarantee', n(g.tests_below)],
          ['Longest run of consecutive days below', g.longest ? `${g.longest.days} days, ${g.longest.first} to ${g.longest.last} (lowest ${n(g.longest.lowest, 0)} Mbps)` : 'none'],
          ['Runs of 3 or more consecutive days', g.qualifying.length ? g.qualifying.map(q => `${q.first} to ${q.last} (${q.days} days)`).join('; ') : 'none']]),
        h('p', { class: 'meta', text: 'A day counts when at least one successful test that day measured below the guaranteed speed. Failed tests (outages) are left out, as the guarantee does not apply to outages. A day without results ends a run.' })));
    }
    paper.append(sec('Latency',
      T([['Idle ping during tests', n(sm.ping.median, 1), n(sm.ping.p['95'], 1), n(sm.ping.max, 1)], ['Jitter during tests', n(sm.jitter.median, 2), n(sm.jitter.p['95'], 2), n(sm.jitter.max, 2)],
        ['External ping (probes)', n(lat.ext_median, 1), n(lat.ext_p95, 1), n(lat.ext_max, 1)], ['Router ping (probes)', n(lat.gw_median, 2), n(lat.gw_p95, 2), '—'],
        ['Packet loss during tests (%)', n(sm.loss.median, 2), n(sm.loss.p['95'], 2), n(sm.loss.max, 2)]], ['ms', 'Median', 'P95', 'Max'])));
    paper.append(sec('Methodology', h('div', { class: 'method' }, ...r.methodology.map(([k, v]) => h('p', {}, h('b', { text: k + '. ' }), v)))));
    paper.append(sec('Integrity', T([['Ledger head at generation', r.ledger.head], ['Ledger entries', n(r.ledger.entries)], ['Export format', r.export_version]]),
      h('p', { class: 'meta', text: 'Raw measurements cannot be edited by the application. Every record is hashed and chained; the raw records behind this report are in the XLSX/CSV export, with their hashes, so they can be checked independently.' })));
  }
  await load();
  return { destroy() { cleanups.forEach(f => f()); } };
}
