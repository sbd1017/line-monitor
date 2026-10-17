// Settings: package, thresholds, schedule (with hard safety limits), engines, display. Every change is logged.
import { api, state, h, clear, section, n, toast, fmtDT, localParts } from '../util.js';
import { refreshSettings } from '../main.js';
import * as bus from '../bus.js';

const GROUPS = [['package', 'Packages'], ['thresholds', 'Other thresholds'], ['schedule', 'Test schedule'], ['probes', 'Connectivity probes'], ['engines', 'Speed-test engines'], ['reset', 'Fresh start'], ['display', 'Display']];

export async function mount(root) {
  const page = h('div', { class: 'page' });
  root.append(page);
  let r = await api('/settings');
  const inputs = {};
  const est = h('div', { class: 'callout', style: { marginTop: '6px' } });
  const status = h('div', {});

  const PKG_FIELDS = [['advertised_down', 'Advertised down (Mbps)'], ['advertised_up', 'Advertised up (Mbps)'],
    ['minimum_guaranteed_down', 'Minimum guaranteed down (optional)'], ['target_down', 'On target at or above'],
    ['degraded_down', 'Degraded below'], ['severe_down', 'Severe below'], ['target_up', 'Upload on target at or above'],
    ['degraded_up', 'Upload degraded below']];

  function packagesEditor(list) {
    const wrap = h('div', { class: 'pkgs' });
    const rows = [];
    const addRow = (p) => {
      const inp = { from: h('input', { type: 'date', value: p.from || '' }), name: h('input', { type: 'text', value: p.name || '', maxlength: 120 }),
        tiers: h('input', { type: 'text', value: Array.isArray(p.tiers) ? p.tiers.join(', ') : (p.tiers || '') }) };
      for (const [k] of PKG_FIELDS) inp[k] = h('input', { type: 'number', step: 'any', min: '0', value: p[k] ?? '' });
      const box = h('div', { class: 'pkg-box' },
        h('div', { class: 'pkg-head' }, h('label', { class: 'field' }, 'Applies from', inp.from), h('label', { class: 'field', style: { flex: 1 } }, 'Name', inp.name),
          h('button', { class: 'btn small danger', type: 'button', text: 'Remove', onclick: () => { if (rows.length > 1) { box.remove(); rows.splice(rows.indexOf(row), 1); } } })),
        h('div', { class: 'form-grid' }, ...PKG_FIELDS.map(([k, label]) => h('label', { class: 'field' }, label, inp[k])),
          h('label', { class: 'field' }, 'Tiers (Mbps, comma separated)', inp.tiers)));
      const row = { box, inp };
      rows.push(row);
      wrap.append(box);
    };
    (list || []).forEach(addRow);
    const add = h('button', { class: 'btn small', type: 'button', text: 'Add a package', onclick: () => {
      const lastP = rows.length ? Object.fromEntries(Object.entries(rows[rows.length - 1].inp).map(([k, el]) => [k, el.value])) : {};
      addRow({ ...lastP, from: '', name: '' }); wrap.append(add);
    } });
    wrap.append(add);
    wrap.getValue = () => rows.map(({ inp }) => {
      const o = { from: inp.from.value, name: inp.name.value.trim(), tiers: inp.tiers.value };
      for (const [k] of PKG_FIELDS) o[k] = inp[k].value === '' ? null : Number(inp[k].value);
      o.tiers = inp.tiers.value.split(/[ ,]+/).filter(Boolean).map(Number);
      return o;
    }).sort((a, b) => a.from.localeCompare(b.from));
    return wrap;
  }

  const control = (f, v) => {
    let el;
    if (f.kind === 'packages') return packagesEditor(v);
    if (f.kind === 'bool') { el = h('input', { type: 'checkbox' }); el.checked = !!v; }
    else if (f.kind === 'choice') { el = h('select', {}, ...f.choices.map(c => h('option', { value: c, text: c }))); el.value = v; }
    else if (f.kind === 'tiers' || f.kind === 'servers') { el = h('input', { type: 'text', value: (v || []).join(', ') }); }
    else if (f.kind === 'datetime') { el = h('input', { type: 'datetime-local' }); if (v) { const p = localParts(Date.parse(v)); el.value = `${p.date}T${String(p.h).padStart(2, '0')}:${String(p.min).padStart(2, '0')}`; } }
    else { el = h('input', { type: f.kind === 'str' ? 'text' : 'number', value: v ?? '', step: f.kind === 'int' ? '1' : 'any', min: f.lo ?? null, max: f.hi ?? null }); }
    el.addEventListener('input', estimate);
    el.addEventListener('change', estimate);
    return el;
  };
  const read = (f, el) => {
    if (f.kind === 'packages') return el.getValue();
    if (f.kind === 'bool') return el.checked;
    if (f.kind === 'tiers' || f.kind === 'servers') return el.value;
    if (f.kind === 'datetime') return el.value ? new Date(el.value).toISOString() : null;
    if (f.kind === 'int' || f.kind === 'float') return el.value === '' ? null : Number(el.value);
    return el.value;
  };

  function render() {
    clear(page);
    page.append(status);
    renderStatus();
    for (const [g, title] of GROUPS) {
      const grid = h('div', { class: 'form-grid' });
      for (const f of r.fields.filter(x => x.group === g)) {
        if (f.key === 'include_legacy' && !state.boot?.bounds?.legacy_tests) continue;   // nothing imported
        const el = control(f, r.settings[f.key]);
        inputs[f.key] = el;
        const lab = f.kind === 'packages'
          ? h('div', { class: 'field', style: { gridColumn: '1 / -1' } }, h('small', { class: 'muted', text: f.help }), el)
          : f.kind === 'bool'
          ? h('label', { class: 'check' }, el, h('span', {}, f.label, f.help ? h('small', { class: 'muted', style: { display: 'block' }, text: f.help }) : null))
          : h('label', { class: 'field' }, f.label, el, f.help ? h('small', { text: f.help }) : null, f.danger_below ? h('small', { class: 'danger-note', text: `Below ${f.danger_below} asks for confirmation.` }) : null);
        grid.append(lab);
      }
      const grp = h('div', { class: 'group' }, h('h3', { text: title }), grid);
      if (g === 'reset') {
        const at = r.settings.fresh_start_at;
        grp.append(h('div', { class: 'callout' + (at ? ' warn' : ''), style: { marginTop: '12px' } },
          at ? `Planned: ${fmtDT(Date.parse(at))}. Everything recorded until then is archived (verified, kept on the Pi) and a new, empty evidence database starts.`
             : 'No fresh start planned.',
          h('div', { style: { marginTop: '10px', display: 'flex', gap: '8px', flexWrap: 'wrap' } },
            h('button', { class: 'btn small danger', type: 'button', text: 'Start fresh now', onclick: async () => {
              const typed = prompt('This archives every current record and starts a new, empty database within 5 minutes.\nType START FRESH to confirm.');
              if (typed !== 'START FRESH') return;
              try { await api('/settings', { body: { changes: { fresh_start_at: new Date().toISOString() }, confirm: true } }); r = await api('/settings'); render(); toast('Fresh start begins within 5 minutes.'); }
              catch (e) { toast(e.message); }
            } }),
            at ? h('button', { class: 'btn small', type: 'button', text: 'Cancel the planned fresh start', onclick: async () => {
              await api('/settings', { body: { changes: { fresh_start_at: null } } }); r = await api('/settings'); render(); toast('Cancelled');
            } }) : null)));
      }
      if (g === 'schedule') grp.append(est, h('p', { class: 'note', text: `Hard limits built into the monitor itself (not editable here): scheduled tests at least ${r.hard.min_interval_min} min apart, burst tests at least ${r.hard.min_burst_interval_min} min apart, at most ${r.hard.max_tests_per_day} tests a day, confirmations at least ${r.hard.min_confirm_delay_s} s apart.` }));
      page.append(grp);
    }
    page.append(h('div', { style: { display: 'flex', gap: '10px', padding: '12px 0 20px' } }, h('button', { class: 'btn primary', text: 'Save settings', onclick: save }), h('button', { class: 'btn', text: 'Revert', onclick: render })));
    const hist = h('table', { class: 'data' }, h('thead', {}, h('tr', {}, ...['When (UTC)', 'Setting', 'From', 'To', 'Source'].map(t => h('th', { text: t })))),
      h('tbody', {}, ...r.history.map(x => h('tr', { style: { cursor: 'default' } }, h('td', { class: 'mono', text: x.changed_at.slice(0, 16).replace('T', ' ') }), h('td', { text: x.key }),
        h('td', { text: x.old_value ?? '—' }), h('td', { text: x.new_value }), h('td', { text: x.source })))));
    page.append(section('', 'Change history', 'every settings change is recorded', h('div', { class: 'table-wrap' }, hist)));
    estimate();
  }

  function renderStatus() {
    const a = r.agent;
    clear(status).append(section('', 'Monitor control', a ? `monitor ${a.online ? 'online' : 'offline'} · ${a.mode} mode · ${a.tests_today} tests today` : 'monitor not seen yet',
      h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' } },
        h('button', { class: 'btn primary', text: 'Run a test now', onclick: async () => { try { await api('/run-test', { body: {} }); toast('Requested. It starts within ~15 seconds.'); bus.kick(); } catch (e) { toast(e.message); } } }),
        h('span', { class: 'lod', style: { marginLeft: '10px' }, text: 'Pause full tests' }),
        ...[[1, '1 h'], [4, '4 h'], [24, '24 h'], [168, '7 days']].map(([hrs, t]) => h('button', { class: 'btn small', text: t, onclick: () => pause(hrs) })),
        h('button', { class: 'btn small', text: 'Resume', onclick: () => pause(0) }),
        h('span', { class: 'muted', text: r.settings.paused_until && Date.parse(r.settings.paused_until) > Date.now() ? `paused until ${fmtDT(Date.parse(r.settings.paused_until))} (probes keep running)` : 'not paused' }))));
  }

  async function pause(hours) {
    await api('/pause', { body: { hours } });
    r = await api('/settings');
    await refreshSettings();
    render();
    toast(hours ? 'Paused' : 'Resumed');
  }

  function estimate() {
    const iv = Number(inputs.interval_min?.value || r.settings.interval_min);
    const perDay = 1440 / Math.max(iv, r.hard.min_interval_min);
    const bytes = r.avg_test_bytes || 1.4e9;
    const day = perDay * bytes;
    clear(est).append(`About ${n(perDay, 0)} scheduled tests a day. At ${n(bytes / 1e6, 0)} MB per test (${r.avg_test_bytes ? 'your recent average' : 'typical for a gigabit test'}) that is ~${n(day / 1e9, 1)} GB a day, ~${n(day * 30 / 1e12, 2)} TB a month, with the line busy ~${n(perDay * 25 / 864, 1)}% of the time. Confirmation and burst tests come on top, up to the daily cap.`);
    est.className = 'callout' + (iv < 15 ? ' warn' : '');
  }

  async function save(confirmDanger = false) {
    const changes = {};
    for (const f of r.fields) {
      const el = inputs[f.key];
      if (!el) continue;
      const v = read(f, el);
      const cur = r.settings[f.key];
      const norm = (x) => JSON.stringify(x, (k, val) => (typeof val === 'number' ? +val : val));
      const same = f.kind === 'packages'
        ? norm([...cur].sort((a, b) => a.from.localeCompare(b.from)).map(p => ({ ...p, tiers: (p.tiers || []).map(Number) }))) === norm(v)
        : JSON.stringify(Array.isArray(cur) ? cur.join(', ') : cur) === JSON.stringify(v) || (f.kind === 'datetime' && !v && !cur);
      if (!same) changes[f.key] = v;
    }
    if (!Object.keys(changes).length) { toast('No changes'); return; }
    try {
      await api('/settings', { body: { changes, confirm: confirmDanger === true } });
    } catch (e) {
      if (e.status === 409 && e.data?.needs_confirmation) {
        if (confirm('These settings make testing more aggressive:\n\n' + e.data.needs_confirmation.join('\n') + '\n\nSave anyway?')) return save(true);
        return;
      }
      toast(e.message); return;
    }
    r = await api('/settings');
    await refreshSettings();
    render();
    toast('Saved. The monitor picks it up within 15 seconds.');
  }
  render();
  return {};
}
