// Export: any period up to ALL data, as a polished XLSX report, raw CSV / CSV.GZ, or a ZIP evidence pack.
import { api, state, h, clear, section, seg, n, toast, BASE, fmtDT, utcMinute } from '../util.js';

const RANGES = [['today', 'Today'], ['yesterday', 'Yesterday'], ['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days'], ['this_month', 'This month'], ['last_month', 'Last month'], ['this_year', 'This year'], ['all', 'ALL DATA'], ['custom', 'Custom']];
const FORMATS = [['xlsx', 'XLSX report'], ['csv', 'CSV'], ['csv.gz', 'CSV.GZ'], ['zip', 'ZIP evidence pack']];

export async function mount(root) {
  const page = h('div', { class: 'page' });
  root.append(page);
  let range = 'all', format = 'xlsx';
  const from = h('input', { type: 'datetime-local' }), to = h('input', { type: 'datetime-local' });
  const customRow = h('div', { style: { display: 'none', gap: '10px', marginTop: '12px', flexWrap: 'wrap' } }, h('label', { class: 'field' }, 'Start', from), h('label', { class: 'field' }, 'End', to));
  const opt = (label, checked, help) => { const i = h('input', { type: 'checkbox' }); i.checked = checked; return [i, h('label', { class: 'check' }, i, h('span', {}, label, help ? h('small', { class: 'muted', style: { display: 'block' }, text: help }) : null))]; };
  const [legacy, legacyL] = opt('Include the legacy speedtest-cli rows', true, 'They are marked source = legacy_csv / legacy_xlsx; statistics follow your Settings.');
  if (!state.boot?.bounds?.legacy_tests) { legacy.checked = false; legacyL.hidden = true; }   // nothing imported: no choice to make
  const [probes, probesL] = opt('Include the 1-minute probes (ZIP only)', true, 'Large: about 0.5 million rows a year.');
  const [rawJson, rawL] = opt('Include engine output (ZIP only)', true);
  const [ip, ipL] = opt('Include the public IP address', false, 'Off by default. Only turn on for a recipient who needs it (e.g. your ISP).');
  const desc = h('div', { class: 'callout', style: { marginTop: '14px' } });
  const describe = () => {
    clear(desc).append(({
      xlsx: 'A designed workbook: Summary (with charts) · Daily · Hourly & Weekday · Incidents · Annotations · Methodology · Integrity · Raw Data. Raw rows continue onto "Raw Data 2", "Raw Data 3"… at Excel\'s 1,048,576-row limit. Nothing is ever left out.',
      csv: 'Every test in the period, one row each, with diagnostics, hashes and ledger positions. A few "#" lines at the top give the generation time, period, record count, version and integrity result.',
      'csv.gz': 'The same CSV, gzip-compressed (typically 5–10× smaller).',
      zip: 'An evidence pack: measurements, probes, events, diagnostics, annotations, your corrections, engine output, ledger, report figures, a manifest and verify_export.py, which re-checks every hash with plain Python.',
    })[format]);
  };
  const go = h('button', { class: 'btn primary', text: 'Create export', onclick: create });
  page.append(section('01', 'Export', 'no row limits · large exports are generated on the Pi and streamed to disk, then downloaded',
    h('div', { class: 'grid' },
      h('div', {}, h('div', { class: 'lod', style: { marginBottom: '8px' }, text: 'Period' }),
        seg(RANGES, range, (v) => { range = v; customRow.style.display = v === 'custom' ? 'flex' : 'none'; }, 'Period'), customRow),
      h('div', {}, h('div', { class: 'lod', style: { marginBottom: '8px' }, text: 'Format' }), seg(FORMATS, format, (v) => { format = v; describe(); }, 'Format'), desc),
      h('div', { class: 'grid', style: { gap: '10px' } }, legacyL, probesL, rawL, ipL),
      h('div', {}, go))));
  describe();
  const list = h('div', {});
  page.append(section('02', 'Recent exports', 'files are kept on the Pi for 24 hours · the SHA-256 lets you match a file you sent to this system later', list));

  async function create() {
    const body = { format, range, include_legacy: legacy.checked, include_probes: probes.checked, include_raw_json: rawJson.checked, include_public_ip: ip.checked };
    if (range === 'custom') { if (!from.value || !to.value) { toast('Choose a start and an end'); return; } body.from = from.value; body.to = to.value; }
    if (ip.checked && !confirm('Include your public IP address in this export?')) return;
    go.disabled = true;
    try { await api('/exports', { body }); toast('Export started'); await refresh(); } catch (e) { toast(e.message); } finally { go.disabled = false; }
  }

  let timer = 0;
  async function refresh() {
    clearTimeout(timer);
    const r = await api('/exports');
    clear(list);
    if (!r.items.length) list.append(h('p', { class: 'muted', text: 'No exports yet.' }));
    let busy = false;
    const t = h('table', { class: 'data' }, h('thead', {}, h('tr', {}, ...['Created', 'Period', 'Format', 'Status', 'Rows', 'Size', 'SHA-256', ''].map(x => h('th', { text: x })))));
    const tb = h('tbody', {});
    for (const e of r.items) {
      busy ||= e.status === 'queued' || e.status === 'running';
      const status = e.status === 'running' ? h('div', { style: { minWidth: '120px' } }, h('div', { class: 'progress' }, h('i', { style: { width: `${Math.round(e.progress * 100)}%` } })), h('small', { class: 'muted', text: `${Math.round(e.progress * 100)}%` }))
        : e.status === 'failed' ? h('span', { class: 'tag fail', text: 'failed', title: e.error || '' }) : h('span', { class: 'tag', text: e.status });
      tb.append(h('tr', { style: { cursor: 'default' } }, h('td', { class: 'mono', text: utcMinute(e.created_at) }), h('td', { text: e.range_label }), h('td', { text: e.format }), h('td', {}, status),
        h('td', { class: 'r', text: e.rows == null ? '' : n(e.rows) }), h('td', { class: 'r', text: e.file_bytes == null ? '' : `${n(e.file_bytes / 1e6, 1)} MB` }),
        h('td', { class: 'hash', title: e.file_sha256 || '', text: e.file_sha256 ? e.file_sha256.slice(0, 16) + '…' : '' }),
        h('td', {}, e.status === 'done' ? h('a', { class: 'btn small primary', href: `${BASE}/exports/${e.uuid}/download`, download: '', text: 'Download' }) : (e.error ? h('small', { class: 'muted', text: e.error.slice(0, 80) }) : null))));
    }
    t.append(tb);
    if (r.items.length) list.append(h('div', { class: 'table-wrap' }, t));
    if (busy) timer = setTimeout(refresh, 1500);
  }
  await refresh();
  return { destroy() { clearTimeout(timer); } };
}
