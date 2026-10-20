// The measurement detail drawer: the raw record, diagnostics, ledger proof, owner corrections, nearby probes.
import { api, h, clear, n, fmtDTs, fmtDT, fmtTime, dur, toast, state, css, utcMinute } from '../util.js';

export function openDrawer(content) {
  const d = document.getElementById('drawer');
  clear(d);
  const close = h('button', { class: 'btn small close', text: 'Close ✕', onclick: closeDrawer });
  d.append(close, content);
  d.hidden = false;
  d.focus?.();
  setTimeout(() => document.addEventListener('keydown', esc), 0);
}
export function closeDrawer() { const d = document.getElementById('drawer'); d.hidden = true; clear(d); document.removeEventListener('keydown', esc); }
function esc(e) { if (e.key === 'Escape') closeDrawer(); }

const kv = (pairs) => h('div', { class: 'kv' }, ...pairs.filter(Boolean).flatMap(([k, v]) => [h('div', { text: k }), h('div', {}, v ?? '—')]));

export async function openTest(uid) {
  openDrawer(h('p', { class: 'muted', text: 'Loading…' }));
  let d;
  try { d = await api('/measurement/' + uid); } catch (e) { openDrawer(h('p', { text: e.message })); return; }
  const m = d.measurement, g = d.diagnostics;
  const ok = m.status === 'ok';
  const box = h('div', {});
  box.append(h('div', { class: 'lod', text: `${m.kind} test · ${m.source}` }),
    h('h2', { style: { fontSize: '30px', margin: '6px 0 2px' }, text: ok ? `${n(m.download_mbps, 1)} / ${n(m.upload_mbps, 1)} Mbps` : `Failed: ${m.failure_code}` }),
    h('p', { class: 'muted', text: `${fmtDTs(m.t_ms)} (${m.tz_name}, UTC${m.utc_offset_min >= 0 ? '+' : ''}${m.utc_offset_min / 60}) · ${m.started_at}` }));
  const q = d.quality;
  if (q && q.assessed) {
    const quiet = q.labels.filter(l => !l.affects_clean), doubt = q.labels.filter(l => l.affects_clean);
    const num = (v, dp = 1) => (v == null ? '—' : n(v, dp));
    // the readings behind each label, from the diagnostics recorded with this test
    const evidence = {
      pi_busy: g && `Other traffic on the Pi: ${num(Math.max(g.background_rx_mbps || 0, g.pretest_rx_mbps || 0))} Mbps in, ${num(Math.max(g.background_tx_mbps || 0, g.pretest_tx_mbps || 0))} Mbps out (during or just before the test).`,
      pi_strained: g && `Load average ${num(g.cpu_load1, 2)}, ${num(g.cpu_temp_c)} °C, firmware flags ${g.throttled || '—'}.`,
      link_errors: g && `Errors ${num(g.rx_errors_delta, 0)} in / ${num(g.tx_errors_delta, 0)} out, drops ${num(g.rx_dropped_delta, 0)} in / ${num(g.tx_dropped_delta, 0)} out.`,
      link_slow: `Link ${m.link_speed_mbps || '?'} Mb/s.`,
      at_link_limit: q.capacity?.ceiling_mbps ? `The link carries about ${n(q.capacity.ceiling_mbps, 0)} Mbps at most.` : '',
    };
    box.append(...[h('h3', { text: 'Measurement quality' }),
      h('p', { class: doubt.length ? 'cap-note' : 'muted', style: { marginTop: 0 }, text: doubt.length
        ? `Not clean: something on the monitor's side could have held this result down, so the clean-only figures leave it out.`
        : 'Clean: nothing on the monitor\'s side could have held this result down.' }),
      ...[...doubt, ...quiet].map(l => h('p', { style: { margin: '6px 0' } }, h('span', { class: 'qtag ' + (l.affects_clean ? 'flag' : 'info'), text: l.label }), ' ',
        h('span', { class: 'muted', text: [l.why, evidence[l.code]].filter(Boolean).join(' ') }))),
      q.capacity && !['ok', 'unknown'].includes(q.capacity.level) ? h('p', { class: 'muted', style: { margin: '6px 0' }, text: q.capacity.text }) : null].filter(Boolean));
  }
  box.append(h('h3', { text: 'Measurement (raw, immutable)' }), kv([
    ['Status', m.status + (m.failure_detail ? ` · ${m.failure_detail}` : '')], ['Download', ok ? `${n(m.download_mbps, 2)} Mbps` : '—'], ['Upload', ok ? `${n(m.upload_mbps, 2)} Mbps` : '—'],
    ['Ping (idle)', m.ping_ms == null ? '—' : `${n(m.ping_ms, 2)} ms (low ${n(m.ping_low_ms, 1)}, high ${n(m.ping_high_ms, 1)})`], ['Jitter', m.jitter_ms == null ? '—' : `${n(m.jitter_ms, 2)} ms`],
    ['Latency under load', m.dl_latency_ms == null ? '—' : `down ${n(m.dl_latency_ms, 1)} ms · up ${n(m.ul_latency_ms, 1)} ms`],
    ['Packet loss', m.packet_loss_pct == null ? 'not reported' : `${n(m.packet_loss_pct, 2)}%`], ['Duration', m.duration_ms ? dur(m.duration_ms) : '—'],
    ['Transferred', m.download_bytes ? `${n(m.download_bytes / 1e6, 0)} MB down · ${n((m.upload_bytes || 0) / 1e6, 0)} MB up` : '—'],
    ['Server', [m.server_sponsor, m.server_location, m.server_country].filter(Boolean).join(' · ') + (m.server_id ? ` (#${m.server_id})` : '')],
    ['Server host', m.server_host], ['Distance', m.distance_km == null ? '—' : `${n(m.distance_km, 0)} km`], ['ISP (engine)', m.isp],
    ['Engine', `${m.engine} ${m.engine_version || ''}`], ['Trigger', m.trigger_reason], ['Result', m.result_url ? h('a', { href: m.result_url, target: '_blank', rel: 'noopener noreferrer', text: m.result_id }) : '—'],
    ['Pi link', m.iface ? `${m.iface} · ${m.link_speed_mbps || '?'} Mb/s ${m.link_duplex || ''} · ${m.link_state || ''}` : '—'],
  ]));
  if (g) {
    box.append(h('h3', { text: 'Diagnostics captured with it (separate record)' }), kv([
      ['Pi CPU load', g.cpu_load1 == null ? '—' : `${n(g.cpu_load1, 2)} (1 min) · ${n(g.cpu_load5, 2)} (5 min)`], ['Pi CPU temperature', g.cpu_temp_c == null ? '—' : `${n(g.cpu_temp_c, 1)} °C`],
      ['Throttling flags', g.throttled == null ? '—' : `${g.throttled}${g.throttled === '0x0' ? ' (none)' : ' (power/thermal limits seen since boot)'}`],
      ['Memory available', g.mem_available_pct == null ? '—' : `${n(g.mem_available_pct, 0)}%`],
      ['Interface errors / drops during test', g.rx_errors_delta == null ? '—' : `rx ${g.rx_errors_delta}/${g.rx_dropped_delta} · tx ${g.tx_errors_delta}/${g.tx_dropped_delta}`],
      ['Other traffic on the Pi (estimate)', g.background_rx_mbps == null ? '—' : `${n(g.background_rx_mbps, 1)} Mbps in · ${n(g.background_tx_mbps, 1)} Mbps out`],
      ['Pi traffic just before', g.pretest_rx_mbps == null ? '—' : `${n(g.pretest_rx_mbps, 1)} in · ${n(g.pretest_tx_mbps, 1)} out (Mbps)`],
      ['Router ping (pre-test)', g.gateway_rtt_ms == null ? '—' : `${n(g.gateway_rtt_ms, 2)} ms, loss ${n(g.gateway_loss_pct, 0)}%`],
      ['External ping (pre-test)', g.external_rtt_ms == null ? '—' : `${n(g.external_rtt_ms, 1)} ms, loss ${n(g.external_loss_pct, 0)}%`],
      ['Public IP', g.public_ip_fingerprint ? `fingerprint ${g.public_ip_fingerprint}${g.public_ip_changed ? ' · CHANGED since the previous test' : ''}` : '—'],
      ['Pi uptime', g.pi_uptime_s == null ? '—' : dur(g.pi_uptime_s * 1000)], ['Agent', g.agent_version],
    ]));
  }
  box.append(h('h3', { text: 'Integrity' }), kv([
    ['Record hash', h('span', { class: 'hash', text: m.record_hash })], ['Recomputed now', d.hash_ok ? '✓ matches' : '✗ DOES NOT MATCH'],
    ['Ledger position', d.ledger ? `#${n(d.ledger.chain_index)} · stored ${d.ledger.recorded_at}` : '—'], ['Chain hash', d.ledger ? h('span', { class: 'hash', text: d.ledger.chain_hash }) : '—'],
    ['Agent sequence', m.agent_seq == null ? '— (import)' : n(m.agent_seq)], ['Record UUID', h('span', { class: 'hash', text: m.uuid })],
    d.derived?.anomaly_z != null ? ['Anomaly score', `z = ${n(d.derived.anomaly_z, 2)} vs median ${n(d.derived.ref_median, 0)} (${d.derived.ref_n} refs)${d.derived.anomaly ? ' · ANOMALY' : ''}`] : null,
  ]));
  if (d.related.length > 1) {
    box.append(h('h3', { text: 'Related tests' }));
    for (const r of d.related.filter(x => x.uuid !== m.uuid)) box.append(h('p', {}, h('a', { href: '#', onclick: (e) => { e.preventDefault(); openTest(r.uuid); }, text: `${fmtDT(r.t_ms)} · ${r.kind} · ${r.status === 'ok' ? n(r.download_mbps, 0) + ' Mbps' : 'failed'}` })));
  }
  if (d.probes.length) {
    box.append(h('h3', { text: 'Probes ±15 minutes' }),
      h('p', { class: 'mono', style: { fontSize: '12px', lineHeight: 1.7 } }, d.probes.map(p => `${fmtTime(p[0])} ${p[1]} ${p[2] == null ? '—' : n(p[2], 1) + 'ms'}`).join('  ·  ')));
  }
  // owner corrections
  const flags = h('div', {});
  box.append(h('h3', { text: 'Your corrections (stored separately; never change the record)' }), flags);
  for (const f of d.flags) {
    flags.append(h('div', { class: 'callout', style: { marginBottom: '8px' } }, h('b', { text: f.flag }), ` · ${f.reason} · added ${utcMinute(f.created_at)}`,
      f.revoked_at ? h('div', { class: 'muted', text: `revoked ${utcMinute(f.revoked_at)}: ${f.revoked_reason}` })
        : h('button', { class: 'btn small', style: { marginLeft: '8px' }, text: 'Revoke', onclick: async () => {
          const reason = prompt('Why revoke this flag?'); if (!reason) return;
          await api(`/flag/${f.id}/revoke`, { body: { reason } }); openTest(uid);
        } })));
  }
  const sel = h('select', {}, h('option', { value: 'exclude', text: 'Exclude from statistics' }), h('option', { value: 'note', text: 'Note' }), h('option', { value: 'disputed', text: 'Disputed' }));
  const why = h('input', { type: 'text', placeholder: 'Reason, e.g. "I was downloading a game on the Pi"', style: { flex: 1 } });
  flags.append(h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '8px' } }, sel, why, h('button', { class: 'btn small', text: 'Add', onclick: async () => {
    try { await api(`/measurement/${uid}/flag`, { body: { flag: sel.value, reason: why.value } }); toast('Saved'); openTest(uid); } catch (e) { toast(e.message); }
  } })));
  if (m.raw) box.append(h('details', {}, h('summary', { text: 'Engine output (private fields removed at source)' }), h('pre', { class: 'raw', text: JSON.stringify(m.raw, null, 1) })));
  openDrawer(box);
}
