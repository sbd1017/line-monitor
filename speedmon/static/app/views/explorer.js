// Data Explorer: every record, filterable and sortable, paged on the server.
import { api, qs, state, h, clear, n, fmtDTs, toast, BASE } from '../util.js';
import { openTest } from './detail.js';

const COLS = [['time', 'Time'], ['download', 'Down'], ['upload', 'Up'], ['ping', 'Ping'], ['jitter', 'Jitter'], ['loss', 'Loss %'], [null, 'Status'], [null, 'Quality'], [null, 'Kind'], ['server', 'Server'], ['anomaly', 'Flags']];

export async function mount(root, params) {
  const st = state.settings;
  const page = h('div', { class: 'page' });
  root.append(page);
  const f = {
    range: h('select', {}, ...[['24h', 'Last 24 hours'], ['7d', 'Last 7 days'], ['30d', 'Last 30 days'], ['90d', 'Last 90 days'], ['1y', 'Last year'], ['all', 'All'], ['custom', 'Custom…']].map(([v, t]) => h('option', { value: v, text: t }))),
    from: h('input', { type: 'datetime-local' }), to: h('input', { type: 'datetime-local' }),
    status: h('select', {}, h('option', { value: '', text: 'Any status' }), h('option', { value: 'ok', text: 'Successful' }), h('option', { value: 'failed', text: 'Failed' })),
    filter: h('select', {}, ...[['', 'All results'], ['below_target', 'Below target'], ['degraded', 'Degraded'], ['severe', 'Severe'], ['anomaly', 'Anomalies'], ['flagged', 'Flagged by you'], ['clean', 'Clean measurements'], ['quality_flagged', 'Possibly affected by the monitor']].map(([v, t]) => h('option', { value: v, text: t }))),
    kind: h('select', {}, ...[['', 'Any kind'], ['scheduled', 'Scheduled'], ['confirmation', 'Confirmation'], ['burst', 'Burst'], ['manual', 'Manual'], ['fallback', 'Fallback'], ['legacy', 'Legacy']].map(([v, t]) => h('option', { value: v, text: t }))),
    source: h('select', {}, h('option', { value: 'agent', text: 'Current monitor' }), h('option', { value: 'legacy', text: 'Legacy import' }), h('option', { value: 'all', text: 'All sources' })),
    server: h('select', {}, h('option', { value: '', text: 'Any server' })),
    dl_min: h('input', { type: 'number', placeholder: 'down ≥', style: { width: '90px' } }), dl_max: h('input', { type: 'number', placeholder: 'down ≤', style: { width: '90px' } }),
    ul_min: h('input', { type: 'number', placeholder: 'up ≥', style: { width: '80px' } }), ul_max: h('input', { type: 'number', placeholder: 'up ≤', style: { width: '80px' } }),
    ping_min: h('input', { type: 'number', placeholder: 'ping ≥', style: { width: '80px' } }), ping_max: h('input', { type: 'number', placeholder: 'ping ≤', style: { width: '80px' } }),
  };
  f.range.value = params.range || '7d';
  let sort = 'time', dir = 'desc', offset = 0, size = 100, total = 0, serversLoaded = false;
  const bar = h('div', { class: 'filters' }, f.range, f.from, f.to, f.status, f.filter, f.kind, f.source, f.server, f.dl_min, f.dl_max, f.ul_min, f.ul_max, f.ping_min, f.ping_max,
    h('button', { class: 'btn small primary', text: 'Apply', onclick: () => { offset = 0; load(); } }));
  for (const el of [f.range, f.status, f.filter, f.kind, f.source, f.server]) el.addEventListener('change', () => { offset = 0; load(); });
  const info = h('div', { class: 'kicker' });
  const thead = h('thead', {});
  const tbody = h('tbody', {});
  const pager = h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center', padding: '14px 0' } });
  page.append(bar, h('div', { style: { height: '14px' } }), info, h('div', { class: 'table-wrap' }, h('table', { class: 'data' }, thead, tbody)), pager);

  function header() {
    clear(thead).append(h('tr', {}, ...COLS.map(([k, t]) => h('th', { class: ['download', 'upload', 'ping', 'jitter', 'loss'].includes(k) ? 'r' : '' },
      k ? h('button', { type: 'button', text: t + (sort === k ? (dir === 'asc' ? ' ↑' : ' ↓') : ''), onclick: () => { if (sort === k) dir = dir === 'asc' ? 'desc' : 'asc'; else { sort = k; dir = 'desc'; } offset = 0; load(); } }) : t))));
  }

  async function load() {
    const q = { range: f.range.value === 'custom' ? 'custom' : f.range.value, from: f.from.value, to: f.to.value, status: f.status.value, filter: f.filter.value, kind: f.kind.value,
      source: f.source.value, server: f.server.value, dl_min: f.dl_min.value, dl_max: f.dl_max.value, ul_min: f.ul_min.value, ul_max: f.ul_max.value, ping_min: f.ping_min.value, ping_max: f.ping_max.value,
      sort, dir, offset, size };
    if (q.range !== 'custom') { delete q.from; delete q.to; }
    page.classList.add('loading');
    let r;
    try { r = await api('/measurements?' + qs(q)); } catch (e) { toast(e.message); return; } finally { page.classList.remove('loading'); }
    total = r.total;
    if (!serversLoaded) { serversLoaded = true; for (const sv of r.servers) f.server.append(h('option', { value: sv.server_id, text: `${sv.server_name || sv.server_id} (${n(sv.n)})` })); }
    header();
    clear(info).append(h('h2', { text: `${n(total)} records` }), h('span', { class: 'sub', text: `showing ${n(Math.min(total, offset + 1))}–${n(Math.min(total, offset + size))} · click a row for the full record` }));
    clear(tbody);
    for (const m of r.items) {
      const ok = m.status === 'ok';
      const band = ok && m.band ? `b-${m.band}` : '';
      const bandUp = ok && m.band_up ? `b-${m.band_up}` : '';
      tbody.append(h('tr', { tabindex: '0', onclick: () => openTest(m.uuid), onkeydown: (e) => { if (e.key === 'Enter') openTest(m.uuid); } },
        h('td', { class: 'mono', text: fmtDTs(m.t_ms) }), h('td', { class: 'r ' + band, text: ok ? n(m.download_mbps, 1) : '—' }), h('td', { class: 'r ' + bandUp, text: ok ? n(m.upload_mbps, 1) : '—' }),
        h('td', { class: 'r' + (m.ping_high ? ' b-below' : ''), text: n(m.ping_ms, 1), title: m.ping_high ? 'Above the latency threshold in Settings' : null }), h('td', { class: 'r', text: n(m.jitter_ms, 2) }), h('td', { class: 'r', text: m.packet_loss_pct == null ? '—' : n(m.packet_loss_pct, 1) }),
        h('td', {}, ok ? 'ok' : h('span', { class: 'tag fail', text: m.failure_code })),
        h('td', {}, ...(m.quality || []).map(c => { const l = (r.quality_labels || {})[c] || {}; return h('span', { class: 'qtag ' + (l.affects_clean ? 'flag' : 'info'), text: l.label || c, title: l.why || '' }); }),
          ok && m.source !== 'legacy_csv' && m.source !== 'legacy_xlsx' && !(m.quality || []).some(c => (r.quality_labels || {})[c]?.affects_clean) && !(m.quality || []).length ? h('span', { class: 'muted', text: 'clean' }) : null),
        h('td', {}, h('span', { class: 'tag' + (m.source.startsWith('legacy') ? ' legacy' : ''), text: m.kind })),
        h('td', { text: m.server_name || m.server_id || '—' }),
        h('td', {}, m.anomaly ? h('span', { class: 'tag anom', text: `anomaly z${n(m.anomaly_z, 1)}` }) : null, m.flags ? h('span', { class: 'tag', text: m.flags }) : null)));
    }
    if (!r.items.length) tbody.append(h('tr', {}, h('td', { colspan: COLS.length, class: 'empty', text: 'No records match.' })));
    clear(pager).append(h('button', { class: 'btn small', text: '← Newer' + (dir === 'asc' ? '/older' : ''), disabled: offset === 0 ? true : null, onclick: () => { offset = Math.max(0, offset - size); load(); } }),
      h('span', { class: 'muted', text: `page ${n(Math.floor(offset / size) + 1)} of ${n(Math.max(1, Math.ceil(total / size)))}` }),
      h('button', { class: 'btn small', text: 'Next →', disabled: offset + size >= total ? true : null, onclick: () => { offset += size; load(); } }),
      h('a', { class: 'btn small', href: `${BASE}/export`, text: 'Export…', style: { marginLeft: 'auto' } }));
  }
  await load();
  if (params.open) openTest(params.open);
  return {};
}
