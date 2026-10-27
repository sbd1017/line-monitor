// Simple: the plain-words view. One answer, three numbers, and what to do about it. The wording comes from the
// backend (speedmon/simple.py), which uses the same rules as the rest of the dashboard.
import { api, BASE, h, clear, seg, toast } from '../util.js';

const PERIODS = [['7d', 'This week'], ['30d', 'This month'], ['90d', 'Last 3 months']];
const ICON = { good: '✓', warn: '!', bad: '✕', unknown: '…' };
const num = (v) => (v == null ? '?' : Math.round(v).toLocaleString('en-GB'));

export async function mount(root, params) {
  let preset = PERIODS.some(([k]) => k === params.range) ? params.range : '30d';
  let alive = true, poll = 0, ctl = null;
  const page = h('div', { class: 'page simple' });
  const body = h('div', { class: 'simple-body', 'aria-live': 'polite' });
  page.append(
    h('div', { class: 'simple-top' },
      h('h1', { text: 'Am I getting what I pay for?' }),
      seg(PERIODS, preset, (v) => { preset = v; load(); }, 'Period')),
    body);
  root.append(page);

  async function load() {
    ctl?.abort();
    ctl = new AbortController();
    body.style.opacity = .5;
    try {
      const r = await api('/simple?range=' + preset, { signal: ctl.signal });
      if (alive) render(r);
    } catch (e) {
      if (e.name !== 'AbortError' && alive) { clear(body).append(h('p', { class: 'muted', text: 'Couldn\'t load this: ' + e.message })); }
    } finally { body.style.opacity = ''; }
  }

  function render(r) {
    clear(body);
    const pk = r.package;
    body.append(
      h('section', { class: `simple-verdict lvl-${r.level}` },
        h('span', { class: 'simple-icon', 'aria-hidden': 'true', text: ICON[r.level] }),
        h('div', {},
          h('h2', { text: r.verdict + '.' }),
          h('p', { text: r.detail }),
          r.since ? h('p', { class: 'muted', text: `This only looks at tests since your package changed on ${r.since}.` }) : null)),

      h('section', { class: 'simple-nums' },
        tile('You pay for', num(pk.advertised_down), short(pk.name)),
        tile('You usually get', num(r.typical_down),
          r.typical_pct == null ? '' : r.typical_pct >= 100 ? 'all of it' : `${Math.min(99, Math.round(r.typical_pct))}% of it`),
        tile('Your slowest', num(r.slowest?.mbps), r.slowest?.when || '')),
      r.typical_up != null ? h('p', { class: 'simple-up muted', text: `Upload (sending files and video calls): you pay for ${num(pk.advertised_up)} Mbps and usually get ${num(r.typical_up)} Mbps.` }) : null,

      h('section', { class: 'simple-rows' },
        row('When is it slow?', r.slow_times.text || 'Not enough tests yet.', r.slow_times.problem ? 'warn' : null),
        row('Does it drop out?', r.dropouts.text, r.dropouts.count ? 'warn' : null),
        row('Virgin\'s promise', [r.promise.text, r.promise.claim_text].filter(Boolean).join(' '), r.promise.can_claim ? 'bad' : null)),

      todo(r),

      h('footer', { class: 'simple-foot muted' },
        h('p', { text: how(r) }),
        h('p', {}, h('a', { href: BASE + '/', text: 'See the detailed view' }), ' · ',
          h('a', { href: `${BASE}/evidence?range=${preset}`, text: 'Full evidence report' }))));
  }

  function todo(r) {
    const s = r.steps;
    const sec = h('section', { class: 'simple-todo' }, h('h2', { text: 'What to do now' }), h('p', { text: s.intro }));
    if (!s.action) return sec;
    const say = h('blockquote', { text: s.say });
    sec.append(
      h('ol', {}, s.list.map(t => h('li', { text: t }))),
      h('div', { class: 'simple-actions' },
        h('a', { class: 'btn primary', href: `${BASE}/evidence?range=${preset}`, text: 'Download your evidence' }),
        sheetButton()),
      h('p', { class: 'muted small', text: 'The evidence report opens as a page: use Print, then Save as PDF. The spreadsheet has every test in it.' }),
      h('div', { class: 'simple-say' },
        h('div', { class: 'simple-say-head' }, h('h3', { text: 'What to say to Virgin' }),
          h('button', { class: 'btn small', type: 'button', text: 'Copy', onclick: () => copy(s.say) })),
        say));
    return sec;
  }

  function sheetButton() {
    const b = h('button', { class: 'btn', type: 'button', text: 'Download a spreadsheet' });
    b.addEventListener('click', async () => {
      b.disabled = true;
      b.textContent = 'Preparing…';
      try {
        const { uuid } = await api('/exports', { body: { format: 'xlsx', range: preset } });
        const started = Date.now();
        const check = async () => {
          if (!alive) return;
          const e = await api('/exports/' + uuid);
          if (e.status === 'done') {
            const a = h('a', { href: `${BASE}/exports/${uuid}/download`, download: '' });
            document.body.append(a); a.click(); a.remove();
            done();
          } else if (e.status === 'failed' || Date.now() - started > 180e3) {
            toast(e.status === 'failed' ? 'The spreadsheet couldn\'t be made. Try the Export tab.' : 'Still working on it. It\'ll appear on the Export tab.');
            done();
          } else poll = setTimeout(check, 1500);
        };
        check();
      } catch (e) { toast(e.message); done(); }
      function done() { b.disabled = false; b.textContent = 'Download a spreadsheet'; }
    });
    return b;
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast('Copied'); } catch { toast('Couldn\'t copy. Select the text and copy it yourself.'); }
  }

  load();
  return { destroy() { alive = false; ctl?.abort(); clearTimeout(poll); } };
}

function tile(label, value, note) {
  return h('div', { class: 'simple-num' },
    h('div', { class: 'simple-num-label', text: label }),
    h('div', { class: 'simple-num-value' }, value, h('span', { text: ' Mbps' })),
    h('div', { class: 'simple-num-note muted', text: note || ' ' }));
}

function row(title, text, tone) {
  return h('div', { class: 'simple-row' + (tone ? ' tone-' + tone : '') }, h('h3', { text: title }), h('p', { text }));
}

function short(name) {
  return (name || '').replace(/^Virgin Media\s+/i, '').replace(/\s+Fibre Broadband$/i, '');
}

function how(r) {
  if (!r.tests) return 'Tests run on their own every 30 minutes. Answers appear here once there are enough of them.';
  let t = `Based on ${r.tests.toLocaleString('en-GB')} regular tests from ${r.period}.`;
  if (r.set_aside) t += ` We left out ${r.set_aside.toLocaleString('en-GB')} where the monitor itself was busy, so they can't count against you or Virgin.`;
  return t + ' Dropouts are counted separately, not as slow tests.';
}
