// Live: the connection right now, then everything the selected period says about it.
import { nextTest, WAITS, api, qs, state, h, clear, section, seg, n, mbps, pct, ms, fmtDT, fmtTime, fmtDay, ago, dur, countdown, RANGES, rangeMs, css, toast, bandOf, BAND_LABEL } from '../util.js';
import * as bus from '../bus.js';
import { sectionNav } from '../sectionnav.js';
import { TimeChart, speedPane, latencyPane, speedTooltip, latencyTooltip } from '../timechart.js';
import { Gauge, Pulse, columns, histogram, percentileStrip, Heatmap, calendarView, Timeline, TL_LABEL, dnaGlyph } from '../viz.js';
import { openTest, openDrawer } from './detail.js';

export async function mount(root, params) {
  const st = state.settings;
  const cleanups = [];
  const page = h('div', { class: 'page' });
  root.append(page);

  // ------------------------------------------------------------ hero
  const dlBig = h('div', { class: 'big' }, h('span', { text: '—' }), h('span', { class: 'u', text: 'Mbps' }));
  const ulBig = h('div', { class: 'big up' }, h('span', { text: '—' }), h('span', { class: 'u', text: 'Mbps' }));
  const dlMeta = h('div', { class: 'meta' });
  const minis = h('div', { class: 'minis' });
  const readout = h('div', { class: 'readout' },
    h('div', { class: 'label' }, h('i', { class: 'key', style: { background: css('--dl') } }), 'Download · latest test'), dlBig, dlMeta,
    h('div', { class: 'label', style: { marginTop: '20px' } }, h('i', { class: 'key', style: { background: css('--ul') } }), 'Upload'), ulBig,
    minis);
  const pkgCap = h('span', { class: 'pkg' });
  const inst = h('div', { class: 'instrument' }, h('span', { class: 'cap', text: 'Line gauge' }), pkgCap);
  const gauge = new Gauge(inst);
  cleanups.push(() => gauge.destroy());
  const side = h('div', { class: 'side' });
  page.append(h('div', { class: 'hero' }, readout, inst, side));
  const ticker = h('div', { class: 'ticker' });
  page.append(ticker);
  const capNote = h('p', { class: 'cap-note', hidden: true });     // monitor capacity vs the package (quality.capacity)
  page.append(capNote);
  if (st.fresh_start_at && Date.parse(st.fresh_start_at) > Date.now()) {
    page.append(h('div', { class: 'trial' }, h('span', { class: 'lod', text: 'Trial period' }),
      `Everything recorded so far is archived and a fresh evidence database starts on ${fmtDT(Date.parse(st.fresh_start_at))}. Change it in Settings → Fresh start.`));
  }

  const pulseBox = h('div', { class: 'pulse' });
  const pulse = new Pulse(pulseBox);
  cleanups.push(() => pulse.destroy());
  const pulseCap = h('span', { class: 'sub' });
  const pulseSec = section('01', 'Network pulse', 'external latency every minute between full tests · last 3 hours', pulseBox);
  pulseSec.right.append(pulseCap);
  page.append(pulseSec);

  let lastTestUuid = null;
  const onLive = (d) => renderHero(d);
  cleanups.push(bus.subscribe(onLive));
  const tickTimer = setInterval(() => { const d = bus.latest(); if (d) renderSide(d); }, 1000);
  cleanups.push(() => clearInterval(tickTimer));

  function renderHero(d) {
    const a = d.agent || {};
    const testing = a.state === 'testing';
    const last = d.last;
    const ok = last && last.status === 'ok';
    inst.classList.toggle('testing', testing);
    const cap = d.capacity;
    capNote.hidden = !cap || ['ok', 'unknown'].includes(cap.level);
    if (!capNote.hidden) { capNote.className = 'cap-note' + (cap.level === 'advertised' ? '' : ' bad'); capNote.textContent = cap.text; }
    gauge.pkg = d.package;
    pkgCap.textContent = d.package ? `${shortName(d.package.name)} · advertised ${d.package.advertised_down}/${d.package.advertised_up} Mbps` : '';
    if (testing && a.live_mbps != null && (a.phase === 'download' || a.phase === 'upload')) {
      gauge.set(a.live_mbps, { live: true, phase: a.phase, progress: a.progress });
    } else if (testing) {
      gauge.set(0, { live: true, phase: a.phase, progress: a.progress });
    } else {
      const lo = d.last_ok;
      gauge.set(lo?.download_mbps ?? 0, { live: false, info: lo ? { pct: lo.pct_of_advertised, mbps: lo.download_mbps, when: `${fmtTime(lo.t_ms)} · ${ago(lo.t_ms).toUpperCase()}` } : {} });
    }
    const dl = ok ? last.download_mbps : null, ul = ok ? last.upload_mbps : null;
    dlBig.firstChild.textContent = last ? (ok ? mbps(dl, 0) : 'FAILED') : '—';
    dlBig.style.color = ok ? (last.band === 'degraded' || last.band === 'severe' ? css('--serious') : '') : last ? css('--crit') : '';
    ulBig.firstChild.textContent = ok ? mbps(ul, ul >= 100 ? 0 : 1) : '—';
    clear(dlMeta);
    if (last) {
      dlMeta.append(h('span', {}, h('b', { text: fmtDT(last.t_ms) }), ` · ${ago(last.t_ms)}`));
      if (ok) {
        const band = last.band || 'below';
        dlMeta.append(h('span', { class: 'vs' }, h('i', { class: `dot ${band === 'target' ? 'good' : band === 'below' ? 'warn' : band === 'degraded' ? 'serious' : 'crit'}` }),
          `${pct(last.pct_of_advertised, 0)} of the advertised ${last.package.advertised_down} Mbps (${shortName(last.package.name)}) · ${BAND_LABEL[band]}`));
      } else dlMeta.append(h('span', { class: 'vs' }, h('i', { class: 'dot crit' }), `${last.failure_code}: ${last.failure_detail || ''}`));
      dlMeta.append(h('span', { text: `${last.server_name || last.server_sponsor || 'server ?'} · ${last.engine}` }));
      if (ok && last.quality) dlMeta.append(qualityTags(last.quality, 'clean measurement'));
      if (last.uuid !== lastTestUuid) { lastTestUuid = last.uuid; dlMeta.onclick = () => openTest(last.uuid); dlMeta.style.cursor = 'pointer'; }
    } else dlMeta.append(h('span', { text: 'Waiting for the first test.' }));
    clear(minis);
    for (const [k, v, u] of [['Ping', ok ? ms(last.ping_ms) : '—', 'ms'], ['Jitter', ok ? ms(last.jitter_ms) : '—', 'ms'],
      ['Loss', ok && last.packet_loss_pct != null ? n(last.packet_loss_pct, 1) : '—', '%'], ['Loaded latency', ok ? ms(last.dl_latency_ms, 0) : '—', 'ms']]) {
      minis.append(h('div', {}, h('div', { class: 'k', text: k }), h('div', { class: 'v' }, v, h('small', { text: u }))));
    }
    clear(ticker);
    const av = d.averages;
    const tick = (k, v, sub) => ticker.append(h('div', {}, h('div', { class: 'k', text: k }), h('div', { class: 'v' }, mbps(v), h('small', { text: 'Mbps' })), h('div', { class: 's', text: sub })));
    tick('Rolling (last 5)', d.rolling5, 'mean of the last five successful tests');
    for (const [key, label] of [['today', 'Today'], ['24h', '24 hours'], ['7d', '7 days'], ['30d', '30 days']]) {
      const x = av?.[key] || {};
      tick(label, x.dl, x.n_ok ? `${n(x.n_ok)} tests · ${pct(x.pct_target, 0)} on target` : 'no tests');
    }
    pulse.set(d.pulse);
    const lp = d.pulse[d.pulse.length - 1];
    pulseCap.textContent = lp ? `now ${lp[1] == null ? 'no reply' : n(lp[1], 1) + ' ms'} · router ${lp[2] == null ? '—' : n(lp[2], 2) + ' ms'} · loss ${n(lp[3] ?? 0, 0)}% · ${lp[4]}` : 'no probes yet';
    renderSide(d);
  }

  function renderSide(d) {
    const a = d.agent;
    clear(side);
    const row = (k, v, sub) => side.append(h('div', { class: 'row' }, h('div', { class: 'k', text: k }), h('div', { class: 'v' }, v), sub ? h('div', { class: 's', text: sub }) : null));
    row('Service', bus.healthPill(d.health), healthText(d));
    if (a?.state === 'testing') {
      row('Now', h('span', { class: 'pill' }, h('i', { class: 'dot live' }), `${a.kind || 'test'} · ${a.phase || ''}`), `${a.engine_name || ''} · started ${ago(a.started_ms)}`);
    } else {
      const next = nextTest(a);
      const capped = a?.next_kind === 'cap';
      row('Next test', h('span', { class: 'countdown', text: a?.paused ? 'paused' : capped ? 'tomorrow' : (Number.isFinite(next) ? countdown(next - Date.now()) : '—') }),
        a ? `${capped ? 'daily limit reached' : !a.next_kind || WAITS.has(a.next_kind) ? 'scheduled' : a.next_kind} · every ${a.interval_min} min · ${a.mode} mode` : 'monitor not seen yet');
    }
    const ln = a?.link || {};
    // The Pi's own cable to the router (negotiated link speed), easily mistaken for the broadband speed.
    const linkOk = ln.state === 'up' && ln.speed >= 1000 && (!ln.duplex || ln.duplex === 'full');
    row('Pi to router', h('span', {}, h('i', { class: `dot ${linkOk ? 'good' : ln.state === 'up' ? 'warn' : ln.state ? 'crit' : 'off'}` }), ' ',
      ln.speed ? (linkOk ? 'cable OK' : `${ln.speed >= 1000 ? ln.speed / 1000 + ' Gb/s' : ln.speed + ' Mb/s'}${ln.duplex && ln.duplex !== 'full' ? ' ' + ln.duplex + ' duplex' : ''}`)
        : (ln.state || 'unknown')),
      !ln.speed ? '' : ln.speed < 1000 ? 'the cable link is below gigabit, so tests can\'t reach a gigabit line: check or replace the cable'
        : ln.duplex && ln.duplex !== 'full' ? 'half duplex: check or replace the cable'
        : `${ln.speed / 1000} Gb/s link on the Pi's Ethernet port. This is the cable, not your broadband speed`);
    row('Monitor', h('span', {}, h('i', { class: `dot ${a?.online ? 'good' : 'off'}` }), ' ', a ? (a.online ? 'online' : 'offline') : 'never seen'),
      a ? `seen ${ago(a.last_seen_ms)} · v${a.version} · queue ${a.queue_depth ?? 0}${a.throttled && a.throttled !== '0x0' ? ' · throttled ' + a.throttled : ''}` : 'start speedagent on the Pi');
    row('Run a test', h('button', { class: 'btn small', onclick: runNow, disabled: a?.state === 'testing' ? true : null, text: 'Test now' }), `${a?.tests_today ?? 0} of ${a?.cap ?? '—'} today`);
  }

  async function runNow() {
    try { await api('/run-test', { body: {} }); toast('Test requested. It starts within ~15 seconds.'); bus.kick(); }
    catch (e) { toast(e.message); }
  }

  // ------------------------------------------------------------ filter row
  const filters = h('div', { class: 'filters' });
  let preset = params.range || st.default_range || '7d';
  let custom = null;
  const fromIn = h('input', { type: 'date', 'aria-label': 'from' });
  const toIn = h('input', { type: 'date', 'aria-label': 'to' });
  const rangeSeg = seg(RANGES, preset, (v) => { preset = v; custom = null; refresh(); }, 'Period');
  filters.append(h('span', { class: 'lod', text: 'Period' }), rangeSeg, fromIn, toIn,
    h('button', { class: 'btn small', text: 'Apply', onclick: () => { if (fromIn.value && toIn.value) { custom = [fromIn.value, toIn.value]; rangeSeg.set(null); refresh(); } } }));
  const legacyBox = h('input', { type: 'checkbox' });
  legacyBox.checked = !!st.include_legacy;
  const hasLegacy = !!state.boot?.bounds?.legacy_tests;            // nothing imported: no toggle, no legend key
  if (hasLegacy) filters.append(h('label', { class: 'check', style: { marginLeft: 'auto' } }, legacyBox, 'show legacy speedtest-cli data on charts'));
  legacyBox.addEventListener('change', () => chart.load());
  page.append(filters);
  const rq = () => custom ? { range: 'custom', from: custom[0], to: custom[1] } : { range: preset };

  // ------------------------------------------------------------ speed history
  const chartBox = h('div', { class: 'chart dim' });
  const zoom = h('div', { class: 'seg' },
    h('button', { type: 'button', text: '−', 'aria-label': 'zoom out', onclick: () => { const [a, b] = chart.range, sp = b - a; chart.setRange(a - sp / 2, b + sp / 2); } }),
    h('button', { type: 'button', text: '+', 'aria-label': 'zoom in', onclick: () => { const [a, b] = chart.range, sp = b - a; chart.setRange(a + sp / 4, b - sp / 4); } }),
    h('button', { type: 'button', text: 'reset', onclick: () => resetChart() }));
  const legend = h('div', { class: 'legend' },
    h('span', {}, h('i', { style: { background: css('--dl') } }), 'Download'), h('span', {}, h('i', { style: { background: css('--ul') } }), 'Upload'),
    h('span', {}, h('i', { class: 'dot', style: { background: css('--serious') } }), 'below degraded'),
    h('span', {}, h('i', { class: 'box', style: { background: css('--crit') } }), 'failed test'),
    h('span', {}, h('i', { class: 'dot', style: { boxShadow: `0 0 0 1.5px ${css('--warn')}` } }), 'anomaly'),
    h('span', {}, h('i', { style: { background: css('--glow') } }), 'your annotation'),
    hasLegacy ? h('span', {}, h('i', { style: { background: css('--faint') } }), 'legacy speedtest-cli (when shown)') : null);
  const chart = new TimeChart(chartBox, {
    label: 'Download and upload speed over time',
    panes: [speedPane('dl', 'Download (Mbps)', '--dl', st, 240), speedPane('ul', 'Upload (Mbps)', '--ul', st, 110)],
    fetch: (a, b, px) => api('/series?' + qs({ from: a, to: b, px, legacy: legacyBox.checked ? 1 : 0 })),
    tooltip: speedTooltip(st),
    onPick: (p) => openTest(p[5]),
    limits: () => rangeMs('all', state.boot.bounds),
  });
  cleanups.push(() => chart.destroy());
  const histSec = section('02', 'Speed history', 'drag to pan · ⌘/Ctrl-scroll or pinch to zoom · double-click to zoom in · click a point for its record',
    h('div', { class: 'chart toolbar' }, legend, h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } }, chart.lod, zoom)), chartBox);
  page.append(histSec);
  function resetChart() {
    let a, b;
    if (custom) { a = Date.parse(custom[0] + 'T00:00:00'); b = Date.parse(custom[1] + 'T23:59:59'); } else [a, b] = rangeMs(preset, state.boot.bounds);
    chart.setRange(a, b);
  }

  // ------------------------------------------------------------ promise kept
  const promise = h('div', { class: 'promise dim' });
  page.append(section('03', 'Promise kept', 'how often the line delivers what the package promises · scheduled tests only', promise));

  // ------------------------------------------------------------ latency
  const latBox = h('div', { class: 'chart dim' });
  const latChart = new TimeChart(latBox, {
    label: 'External latency from the probes', panes: [latencyPane(st, 150)],
    fetch: (a, b, px) => api('/latency?' + qs({ from: a, to: b, px })), tooltip: latencyTooltip(),
    limits: () => rangeMs('all', state.boot.bounds),
  });
  cleanups.push(() => latChart.destroy());
  const latStats = h('div', { class: 'streaks' });
  page.append(section('04', 'Latency', 'probe latency to external anchors (red = Internet unreachable) · speed-test ping & jitter below',
    h('div', { class: 'chart toolbar' }, h('div', { class: 'legend' }, h('span', {}, h('i', { style: { background: css('--lat') } }), 'external RTT (median of anchors)'),
      h('span', {}, h('i', { class: 'box', style: { background: css('--crit') } }), 'outage'), h('span', {}, h('i', { class: 'box', style: { background: css('--offline') } }), 'Pi/LAN fault')), latChart.lod),
    latBox, latStats));

  // ------------------------------------------------------------ reliability
  const tlBox = h('div', { class: 'timeline dim' });
  const tl = new Timeline(tlBox);
  cleanups.push(() => tl.destroy());
  const tlLegend = h('div', { class: 'legend', style: { marginTop: '10px' } });
  for (const [k, c] of [['ok', '--dl'], ['latency', '--warn'], ['outage', '--crit'], ['local', '--serious'], ['offline', '--offline'], ['unknown', '--surface-2']]) {
    tlLegend.append(h('span', {}, h('i', { class: 'box', style: { background: css(c) } }), TL_LABEL[k]));
  }
  const incidents = h('div', { class: 'grid g2', style: { marginTop: '18px' } });
  page.append(section('05', 'Reliability', 'what the line was doing between tests · a powered-off Pi shows as monitor offline, never as an ISP outage', tlBox, tlLegend, incidents));

  // ------------------------------------------------------------ patterns
  const heatBox = h('div', { class: 'heat dim' });
  const heat = new Heatmap(heatBox, { onDay: (d) => window.__go('/day/' + d) });
  cleanups.push(() => heat.destroy());
  const hourBox = h('div', { class: 'dim' }), dowBox = h('div', { class: 'dim' }), congBox = h('div', {});
  page.append(section('06', 'When does it slow down?', 'scheduled tests in local time',
    congBox,
    h('div', { class: 'grid g-7-5', style: { marginTop: '18px' } },
      h('div', { class: 'block' }, h('h3', { text: 'Day × hour' }), h('div', { class: 'cap', text: 'mean download per hour of each day · click a row to open that day' }), heatBox),
      h('div', { class: 'grid' },
        h('div', { class: 'block' }, h('h3', { text: 'Hour of day' }), h('div', { class: 'cap', text: 'median download per hour · tick = P10 (90% of tests were faster)' }), hourBox),
        h('div', { class: 'block' }, h('h3', { text: 'Day of week' }), h('div', { class: 'cap', text: 'median download · tick = P10' }), dowBox)))));

  // ------------------------------------------------------------ distribution
  const histBox = h('div', { class: 'dim' }), pBox = h('div', { class: 'dim' }), pUpBox = h('div', { class: 'dim' }), histUpBox = h('div', { class: 'dim' });
  page.append(section('07', 'Distribution', 'where results cluster · percentiles shown only where the sample supports them',
    h('div', { class: 'grid g2' },
      h('div', { class: 'block' }, h('h3', { text: 'Download distribution' }), h('div', { class: 'cap', text: 'share of scheduled tests per 25 Mbps band' }), histBox,
        h('h3', { style: { marginTop: '18px' }, text: 'Download percentiles' }), h('div', { class: 'cap', text: 'dark box P25–P75 · light box P10–P90' }), pBox),
      h('div', { class: 'block' }, h('h3', { text: 'Upload distribution' }), h('div', { class: 'cap', text: 'share of scheduled tests per 5 Mbps band' }), histUpBox,
        h('h3', { style: { marginTop: '18px' }, text: 'Upload percentiles' }), h('div', { class: 'cap', text: ' ' }), pUpBox))));

  // ------------------------------------------------------------ calendar + DNA + anomalies
  const calBox = h('div', { class: 'cal' });
  const calLegend = h('div', { class: 'legend', style: { marginTop: '8px' } });
  for (const [k, c] of [['median on target', '--div-p3'], ['just below target', '--div-p1'], ['at the degraded threshold', '--div-0'], ['degraded', '--div-n2'], ['severe / failed', '--div-n3'], ['no data', '--surface-2']]) calLegend.append(h('span', {}, h('i', { class: 'box', style: { background: css(c) } }), k));
  calLegend.append(h('span', {}, h('i', { class: 'dot', style: { background: css('--crit') } }), 'outage ≥ 2 min'));
  page.append(section('08', 'Every day', 'each square is a day, coloured by its median download · click to drill in', calBox, calLegend));
  const dnaRow = h('div', { class: 'dna-row' });
  page.append(section('09', 'Connection DNA', 'a fingerprint per period: spoke length = median speed per hour, ribbon = P10–P90, thickness = jitter, core ring = latency, red dots = failures · click for the numbers', dnaRow));
  const anomBox = h('div', { class: 'list' });
  const ANOM_FEW = 3;                                     // the latest few; the rest behind Show more
  let anomOpen = false;
  page.append(section('10', 'Anomalies', 'tests far outside what is normal for that hour and day type (robust z ≥ 3.5 against the previous 28 days)', anomBox));

  // ------------------------------------------------------------ data
  async function refresh() {
    page.classList.add('loading');
    resetChart();
    latChart.setRange(...chart.range);
    const q = qs(rq());
    try {
      const [sm, tlD, hm, cg, an] = await Promise.all([api('/summary?' + q), api('/timeline?' + q), api('/heatmap?' + q), api('/congestion?' + q), api('/anomalies?' + q)]);
      const pk = { ...st, ...sm.summary.package };
      renderPromise(sm, pk);
      renderLatencyStats(sm.summary);
      tl.set(tlD);
      renderIncidents(tlD);
      heat.set(hm.rows.slice(-400), st);
      columns(hourBox, sm.hourly, { labelOf: (r) => String(r.hour).padStart(2, '0'), st: pk,
        tipRows: (r) => [{ label: 'Median', value: `${n(r.median, 0)} Mbps` }, { label: 'P10', value: `${n(r.p10, 0)} Mbps` }, { label: 'On target', value: pct(r.pct_target, 0) }, { label: 'Tests', value: n(r.n_ok) }] });
      columns(dowBox, sm.weekday, { labelOf: (r) => r.name, st: pk, highlightWorst: 1, height: 180,
        tipRows: (r) => [{ label: 'Median', value: `${n(r.median, 0)} Mbps` }, { label: 'P10', value: `${n(r.p10, 0)} Mbps` }, { label: 'On target', value: pct(r.pct_target, 0) }, { label: 'Tests', value: n(r.n_ok) }] });
      histogram(histBox, sm.histogram, pk);
      histogram(histUpBox, sm.histogram_up, pk, { color: '--ul', target: pk.target_up });
      percentileStrip(pBox, sm.summary.download, pk);
      percentileStrip(pUpBox, sm.summary.upload, { ...pk, target_down: pk.target_up, advertised_down: pk.advertised_up });
      renderCongestion(cg);
      renderAnomalies(an);
    } finally { page.classList.remove('loading'); }
  }
  async function refreshFixed() {
    const cal = await api('/calendar?range=1y');
    calendarView(calBox, cal.days, st, (d) => window.__go('/day/' + d));
    clear(dnaRow);
    for (const [label, rng] of [['Today', { range: 'today' }], ['Last 7 days', { range: '7d' }], ['Last 30 days', { range: '30d' }]]) {
      const d = await api('/dna?' + qs(rng));
      const cv = h('canvas');
      const cardEl = h('div', { class: 'dna', tabindex: '0', role: 'button' }, cv, h('div', { class: 't', text: label }),
        h('div', { class: 's', text: d.summary.n_ok ? `${pct(d.summary.pct_target, 0)} on target · ${n(d.summary.n_ok)} tests` : 'no tests yet' }));
      dnaRow.append(cardEl);
      requestAnimationFrame(() => dnaGlyph(cv, d, st, Math.max(120, Math.min(300, (cardEl.clientWidth || 300) - 20))));
      cardEl.addEventListener('click', () => dnaDetail(label, d));
    }
  }

  function dnaDetail(label, d) {
    const sm = d.summary;
    openDrawer(h('div', {}, h('h2', { text: `Connection DNA · ${label}` }), h('p', { class: 'note', text: d.range.label }),
      h('div', { class: 'kv', style: { marginTop: '14px' } },
        ...[['Successful tests', n(sm.n_ok)], ['Median download', `${n(sm.median, 0)} Mbps`], ['Mean download', `${n(sm.mean, 0)} Mbps`],
          ['P10 (90% faster than)', `${n(sm.p10, 0)} Mbps`], ['On target', pct(sm.pct_target)], ['Failed (ISP side)', pct(sm.pct_failed)],
          ['Median ping', `${n(sm.ping, 1)} ms`], ['Median jitter', `${n(sm.jitter, 2)} ms`], ['Stability (1 − (P90−P10)/median)', sm.stability == null ? '—' : n(sm.stability, 2)]]
          .flatMap(([k, v]) => [h('div', { text: k }), h('div', { text: v })])),
      h('h3', { style: { margin: '20px 0 8px' }, text: 'Per hour' }),
      h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ...['Hour', 'Tests', 'Median', 'P10', 'P90', 'Ping', 'Jitter', 'Failed'].map(t => h('th', { text: t })))),
        h('tbody', {}, ...d.spokes.map(sp => h('tr', {}, h('td', { text: `${String(sp.hour).padStart(2, '0')}:00` }), h('td', { text: n(sp.n) }), h('td', { text: n(sp.median, 0) }),
          h('td', { text: n(sp.p10, 0) }), h('td', { text: n(sp.p90, 0) }), h('td', { text: n(sp.ping, 1) }), h('td', { text: n(sp.jitter, 2) }), h('td', { text: n(sp.failed) }))))))));
  }

  function renderPromise(d, st) {
    const sm = d.summary;
    clear(promise);
    if (sm.packages && sm.packages.length > 1) {
      promise.append(h('div', { class: 'callout warn', style: { gridColumn: '1 / -1' } },
        `This period spans a package change (${sm.packages.map(p => `${p.name} from ${p.from}`).join(' → ')}). Every test is judged against the package it was on; `
        + `the tier bars use ${sm.tiers_package}. Use Compare for a clean before/after.`));
    }
    const score = sm.pct_target;
    const verdict = sm.n_ok ? `Of ${n(sm.n)} scheduled tests ${d.range.label.toLowerCase()}, ${pct(score, 1)} reached their package's target (${st.target_down} Mbps on ${st.name}); the typical result was ${n(sm.download.median, 0)} Mbps, ${pct(sm.pct_of_advertised_median, 0)} of the advertised speed.` : 'No successful scheduled tests in this period yet.';
    const qs_ = sm.quality;
    const cleanLine = !qs_ || !qs_.n_ok ? null : qs_.n_flagged
      ? `Setting aside ${n(qs_.n_flagged)} test${qs_.n_flagged > 1 ? 's' : ''} the monitor itself may have affected (${Object.entries(qs_.flags).filter(([c]) => qs_.labels[c]).map(([c, k]) => `${qs_.labels[c]}: ${k}`).join(', ')}): ${pct(qs_.clean.pct_target, 1)} on target.`
      : `All ${n(qs_.n_ok)} successful tests were clean measurements: nothing on the monitor's side held them down.`;
    promise.append(h('div', {},
      h('div', { class: 'lod', text: 'promise kept' }),
      h('div', { class: 'score' }, score == null ? '—' : n(score, 1), h('small', { text: '%' })),
      h('p', { class: 'verdict', text: verdict }),
      cleanLine ? h('p', { class: 'note', text: cleanLine }) : null));
    const tiers = h('div', { class: 'tiers' });
    const bar = (label, v, cls, sub) => tiers.append(h('div', { class: 'tier' }, h('span', { class: 't', text: label }),
      h('div', { class: 'bar', title: sub || '' }, h('i', { class: cls || '', style: { width: `${Math.max(0, Math.min(100, v || 0))}%` } })), h('span', { class: 'p', text: v == null ? '—' : pct(v) })));
    for (const t of sm.tiers) bar(`≥ ${t.mbps} Mbps`, t.pct);
    bar(`< ${st.degraded_down} degraded`, sm.pct_degraded, 'serious');
    bar(`< ${st.severe_down} severe`, sm.pct_severe, 'crit');
    bar('failed (ISP side)', sm.pct_failed, 'crit');
    bar('outage at test time', sm.pct_outage, 'off');
    const s3 = sm.streaks;
    const sk = h('div', { class: 'streaks' });
    const cur = s3.current;
    const desc = { good: 'on target', poor: 'degraded or failed', failed: 'failed', between: 'below target' };
    sk.append(h('div', {}, h('div', { class: 'k', text: 'Current streak' }), h('div', { class: 'v', text: cur ? `${n(cur.count)} ${desc[cur.kind]}` : '—' }), h('div', { class: 's', text: cur ? `since ${fmtDT(cur.first_ms)}` : '' })));
    const g = s3.longest_good, p = s3.longest_poor;
    sk.append(h('div', {}, h('div', { class: 'k', text: 'Longest run on target' }), h('div', { class: 'v', text: g ? `${n(g.count)} tests` : '—' }), h('div', { class: 's', text: g ? `${fmtDT(g.first_ms)} → sampled span ${dur(g.sampled_span_ms)}` : '' })));
    sk.append(h('div', {}, h('div', { class: 'k', text: 'Longest poor run' }), h('div', { class: 'v', text: p ? `${n(p.count)} tests` : 'none' }), h('div', { class: 's', text: p ? `${fmtDT(p.first_ms)} → sampled span ${dur(p.sampled_span_ms)}` : '' })));
    const gu = d.guarantee;
    let gw = null;
    if (gu && gu.applies) {
      const run = gu.current ? gu.current.days : 0;
      const qual = gu.qualifying.length;
      gw = h('div', { class: 'guarantee' + (qual ? ' hit' : run ? ' warn' : '') },
        h('div', { class: 'k', text: `Guarantee watch · ${gu.mgds} Mbps minimum` }),
        h('div', { class: 'v' }, qual ? `${qual} qualifying run${qual > 1 ? 's' : ''}` : `${run} of 3 days`),
        h('div', { class: 's', text: qual
          ? `Below ${gu.mgds} Mbps on ${gu.qualifying.map(q => `${q.days} consecutive days from ${q.first}`).join('; ')}. Your contract lets you report this; if it isn't fixed within 30 days you may leave without a fee.`
          : run ? `Below the guaranteed ${gu.mgds} Mbps on ${run} consecutive day${run > 1 ? 's' : ''} so far (lowest ${n(gu.current.lowest, 0)} Mbps). Three in a row qualify under your contract.`
            : `No day in this period has a result below the guaranteed ${gu.mgds} Mbps${gu.days_below ? ` right now (${gu.days_below} day${gu.days_below > 1 ? 's' : ''} earlier in the period)` : ''}.` }),
        h('div', { class: 'gdays' }, ...gu.recent.map(x => h('i', { class: x.below ? 'b' : '', title: `${x.day}: ${x.below} of ${x.n} tests below ${gu.mgds}${x.lowest != null ? `, lowest ${n(x.lowest, 0)}` : ''}` }))));
    }
    promise.append(h('div', {}, tiers, sk, gw,
      h('p', { class: 'note', text: `Coverage: monitor running ${pct(d.coverage.pct, 1)} of the period; ${n(d.coverage.tests_scheduled)} of ~${n(d.coverage.tests_expected)} scheduled tests. ${n(sm.n_all - sm.n)} confirmation/burst/manual tests are excluded from these shares${sm.excluded ? `; ${n(sm.excluded)} owner-excluded` : ''}.` })));
  }

  function renderLatencyStats(sm) {
    clear(latStats);
    for (const [k, d, u] of [['Speed-test ping (median)', sm.ping, 'ms'], ['Jitter (median)', sm.jitter, 'ms'], ['Packet loss (mean)', sm.loss, '%']]) {
      latStats.append(h('div', {}, h('div', { class: 'k', text: k }), h('div', { class: 'v', text: d.median == null ? '—' : `${n(k.includes('loss') ? d.mean : d.median, 2)} ${u}` }),
        h('div', { class: 's', text: d.n ? `min ${n(d.min, 1)} · max ${n(d.max, 1)} · P95 ${n(d.p['95'], 1)}` : '' })));
    }
  }

  function renderIncidents(d) {
    clear(incidents);
    const list = (title, items, fmt) => {
      const box = h('div', { class: 'block' }, h('h3', { text: title }));
      if (!items.length) box.append(h('p', { class: 'muted', text: 'None in this period.' }));
      const l = h('div', { class: 'list' });
      for (const it of items.slice(-8).reverse()) l.append(fmt(it));
      box.append(l);
      incidents.append(box);
    };
    list(`Confirmed Internet outages (${d.outages.length})`, d.outages, (o) => h('div', { class: 'item', onclick: () => chart.setRange(o.first_ms - 3600e3, o.last_ms + 3600e3) },
      h('span', { class: 'mono', text: fmtDT(o.first_ms) }), h('span', { text: `${dur(o.min_duration_ms)} to ${dur(o.max_duration_ms)} · ${o.probes} probes` }), h('span', { class: 'tag fail', text: o.states.join('/') })));
    list(`Degradation incidents (${d.degradations.length})`, d.degradations, (x) => h('div', { class: 'item', onclick: () => chart.setRange(x.first_ms - 3600e3, x.last_ms + 3600e3) },
      h('span', { class: 'mono', text: fmtDT(x.first_ms) }), h('span', { text: `${x.count} tests · sampled span ${dur(x.sampled_span_ms)} · low ${n(x.min_dl, 0)} Mbps` }), h('span', { class: 'tag', text: `${x.failed} failed` })));
  }

  function renderCongestion(cg) {
    clear(congBox);
    if (cg.status === 'insufficient' || !cg.findings.length) {
      congBox.append(h('div', { class: 'finding none' }, h('div', { class: 'h', text: cg.status === 'insufficient' ? 'Congestion detector: gathering data' : 'Congestion detector: no recurring slowdown' }), h('div', { class: 'd', text: cg.message })));
      return;
    }
    for (const f of cg.findings) {
      congBox.append(h('div', { class: 'finding' }, h('div', { class: 'h', text: `Typical ${f.daytype} degradation ${f.start}–${f.end} · −${n(f.drop_pct, 0)}%` }), h('div', { class: 'd', text: f.text })));
    }
  }

  function renderAnomalies(an) {
    clear(anomBox);
    if (!an.items.length) { anomBox.append(h('p', { class: 'muted', text: 'No anomalous tests in this period.' })); return; }
    const items = an.items.slice(0, 30);
    for (const a of anomOpen ? items : items.slice(0, ANOM_FEW)) {
      anomBox.append(h('div', { class: 'item', onclick: () => openTest(a.uuid) }, h('span', { class: 'mono', text: fmtDT(a.t_ms) }),
        h('span', { text: `${n(a.download_mbps, 0)} Mbps against a usual ${n(a.ref_median, 0)} for that hour (${a.ref_n} reference tests)` }),
        h('span', { class: 'tag anom', text: `z ${n(a.anomaly_z, 1)}` })));
    }
    if (items.length > ANOM_FEW) {
      anomBox.append(h('button', { class: 'btn small list-more', type: 'button', 'aria-expanded': String(anomOpen),
        text: anomOpen ? 'Show fewer' : `Show ${items.length - ANOM_FEW} more`,
        onclick: () => { anomOpen = !anomOpen; renderAnomalies(an); } }));
    }
  }

  await refresh();
  refreshFixed();
  // ------------------------------------------------------------ section dock
  const sec = (title) => [...page.querySelectorAll(':scope > .section')].find(s => s.querySelector('h2')?.textContent === title);
  const groups = [
    ['live', 'Live', page.querySelector(':scope > .hero')],
    ['history', 'History', sec('Network pulse')],
    ['promise', 'Promise', sec('Promise kept')],
    ['reliability', 'Reliability', sec('Latency')],
    ['patterns', 'Patterns', sec('When does it slow down?')],
    ['dna', 'DNA', sec('Connection DNA')],
  ].filter(g => g[2]).map(([id, label, el]) => ({ id, label, el }));
  const nav = sectionNav(page, groups);
  cleanups.push(() => nav.destroy());
  return { destroy() { cleanups.forEach(f => f()); } };
}

// Measurement-quality tags for one test ({codes, clean, labels} from the API). Informational labels look quieter.
export function qualityTags(q, cleanText) {
  const wrap = h('span', { class: 'qtags' });
  if (q.clean && !q.labels.some(l => !l.affects_clean)) wrap.append(h('span', { class: 'qtag', text: cleanText || 'clean', title: 'Nothing on the monitor\'s side could have held this result down.' }));
  for (const l of q.labels) wrap.append(h('span', { class: 'qtag ' + (l.affects_clean ? 'flag' : 'info'), text: l.label, title: l.why }));
  return wrap;
}

export function shortName(name) {
  return (name || '').replace(/^Virgin Media\s+/i, '').replace(/\s+Fibre Broadband$/i, '') || name;
}

function healthText(d) {
  const a = d.agent;
  return ({ ok: 'Internet reachable, latency normal', latency: 'Reachable but latency is high', degraded: 'Latest test below the degraded threshold',
    outage: 'Router answers but the Internet does not', local: 'The Pi or router side has a problem', monitor_offline: `No word from the Pi since ${a ? ago(a.last_seen_ms) : '—'}`,
    stale: 'No probe in the last 5 minutes', unknown: 'Waiting for the first probe' })[d.health] || '';
}
