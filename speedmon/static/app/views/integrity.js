// Integrity: ledger head, a full verification on demand, delivery gaps, quarantine, imports and export fingerprints.
import { api, h, clear, section, n, toast, utcMinute } from '../util.js';

export async function mount(root) {
  const page = h('div', { class: 'page' });
  root.append(page);
  return mountInto(page);
}

// Also used at the bottom of the Evidence page.
export async function mountInto(container) {
  const body = h('div', {});
  container.append(body);
  let timer = 0;
  async function load() {
    clearTimeout(timer);
    const r = await api('/integrity');
    clear(body);
    const head = r.head;
    const copy = h('button', { class: 'btn small', text: 'Copy', onclick: () => navigator.clipboard.writeText(head.head).then(() => toast('Copied')) });
    const verify = h('button', { class: 'btn primary', text: r.running ? 'Verifying…' : 'Verify everything now', disabled: r.running ? true : null, onclick: async () => { await api('/integrity/verify', { body: {} }); load(); } });
    const last = r.last;
    if (r.epoch) {
      const e = r.epoch;
      body.append(h('div', { class: 'callout', style: { marginTop: '26px' } }, h('b', { text: `Fresh start on ${new Date(e.epoch_ms).toLocaleString('en-GB')}` }),
        ` (${e.reason}). Everything before it was archived, verified, to ${e.archive}: ${n(e.counts?.measurements)} tests, ${n(e.ledger_entries)} ledger entries.`,
        h('div', { class: 'hash', text: `archive SHA-256 ${e.archive_sha256}` }), h('div', { class: 'hash', text: `previous ledger head ${e.ledger_head}` })));
    }
    body.append(section('01', 'Ledger', 'every evidence record is hashed and chained in the order it was stored',
      h('div', { class: 'kv' }, h('div', { text: 'Head (chain hash)' }), h('div', {}, h('span', { class: 'hash', text: head.head }), ' ', copy),
        h('div', { text: 'Entries' }), h('div', { text: n(head.entries) }), h('div', { text: 'Last entry' }), h('div', { text: head.at || '—' })),
      h('p', { class: 'note', text: 'Write the head down now and then, or keep an export: if any earlier record were ever changed, the head would no longer match. This is tamper-evidence, not a legal certification.' }),
      h('div', { style: { marginTop: '16px', display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap' } }, verify,
        last ? h('span', { class: 'pill' }, h('i', { class: `dot ${last.ok ? 'good' : 'crit'}` }), last.ok ? `passed · ${n(last.records)} records · ${utcMinute(last.checked_at)} UTC` : `FAILED · ${last.problems.length} problem(s)`) : h('span', { class: 'muted', text: 'not verified since the server started' })),
      last && !last.ok ? h('pre', { class: 'raw', text: last.problems.join('\n') }) : null));
    const seqs = h('div', { class: 'list static' });
    for (const g of r.sequence) seqs.append(h('div', { class: 'item' }, h('span', { class: 'mono', text: g.agent_id }),
      h('span', { text: `sequence ${n(g.first)}–${n(g.last)} · ${n(g.received)} received · ${n(g.missing)} never received · ${n(g.quarantined)} quarantined` }),
      h('span', { class: 'tag' + (g.missing ? ' fail' : ''), text: g.missing ? 'gaps' : 'complete' })));
    const q = r.agent_queue;
    body.append(section('02', 'Delivery', 'records are numbered by the monitor, so a lost record shows up as a gap', seqs,
      q ? h('p', { class: 'note', text: `Waiting in the Pi's outbox right now: ${n(q.depth)}${q.oldest_ms ? ` (oldest from ${new Date(q.oldest_ms).toLocaleString('en-GB')})` : ''}. Rejected locally: ${n(q.rejected)}.` }) : null));
    const qt = h('div', { class: 'list static' });
    if (!r.quarantine.length) qt.append(h('p', { class: 'muted', text: 'Nothing quarantined.' }));
    for (const x of r.quarantine) qt.append(h('div', { class: 'item' }, h('span', { class: 'mono', text: x.received_at.slice(0, 16) }), h('span', { text: `${x.stream || '?'} ${x.uuid || ''}: ${x.error}` }), h('span', {})));
    body.append(section('03', 'Quarantine', 'submissions that failed validation or hash checks are kept, never dropped', qt));
    const im = h('div', { class: 'list static' });
    for (const x of r.imports) im.append(h('div', { class: 'item' }, h('span', { class: 'mono', text: x.started_at.slice(0, 16) }),
      h('span', {}, h('b', { text: x.file_name }), ` · ${x.format} · ${n(x.rows_read)} read, ${n(x.inserted)} imported, ${n(x.duplicates)} duplicates, ${n(x.repaired)} repaired, ${n(x.rejected)} rejected`, h('div', { class: 'hash', text: 'SHA-256 ' + x.file_sha256 })), h('span', {})));
    if (r.imports.length) body.append(section('04', 'Imported history', 'legacy files are read, never modified; each import is fingerprinted', im));
    const ex = h('div', { class: 'list static' });
    if (!r.exports.length) ex.append(h('p', { class: 'muted', text: 'No exports yet.' }));
    for (const x of r.exports) ex.append(h('div', { class: 'item' }, h('span', { class: 'mono', text: x.created_at.slice(0, 16) }),
      h('span', {}, `${x.format} · ${x.range_label} · ${n(x.rows)} rows`, h('div', { class: 'hash', text: `file SHA-256 ${x.file_sha256}` }), h('div', { class: 'hash', text: `ledger head ${x.ledger_head}` })), h('span', {})));
    body.append(section('05', 'Export fingerprints', 'to match a file someone sends back to you', ex));
    if (r.running) timer = setTimeout(load, 2000);
  }
  await load();
  return { destroy() { clearTimeout(timer); } };
}
