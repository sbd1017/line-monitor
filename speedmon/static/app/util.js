// Shared helpers: API, formatting, DOM, tooltip. No dependencies; strings from data always go in via textContent.

export const BASE = document.querySelector('meta[name="base"]').content;
const CSRF = document.querySelector('meta[name="csrf"]').content;
export const state = { boot: null, tz: 'Europe/London', settings: null };

export async function api(path, opts = {}) {
  const init = { credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (opts.body !== undefined) {
    init.method = 'POST';
    init.headers['Content-Type'] = 'application/json';
    init.headers['X-CSRF-Token'] = CSRF;
    init.body = JSON.stringify(opts.body);
  }
  if (opts.signal) init.signal = opts.signal;
  const r = await fetch(BASE + '/api' + path, init);
  if (r.status === 401) { location.href = BASE + '/enter'; throw new Error('signed out'); }
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (!r.ok) { const e = new Error((data && data.error) || `HTTP ${r.status}`); e.status = r.status; e.data = data; throw e; }
  return data;
}

export function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') u.set(k, v);
  return u.toString();
}

// ---------------------------------------------------------------- formatting

const nf = {};
export function n(v, d = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const key = d;
  nf[key] ??= new Intl.NumberFormat('en-GB', { minimumFractionDigits: d, maximumFractionDigits: d });
  return nf[key].format(v);
}
export const mbps = (v, d) => (v === null || v === undefined) ? '—' : n(v, d ?? (v >= 100 ? 0 : 1));
export const pct = (v, d = 1) => (v === null || v === undefined) ? '—' : n(v, d) + '%';
export const ms = (v, d = 1) => (v === null || v === undefined) ? '—' : n(v, d);

const dtf = {};
function fmtr(opts) {
  const key = JSON.stringify(opts) + state.tz;
  dtf[key] ??= new Intl.DateTimeFormat('en-GB', { timeZone: state.tz, ...opts });
  return dtf[key];
}
export const fmtTime = (t) => fmtr({ hour: '2-digit', minute: '2-digit' }).format(t);
export const fmtTimeS = (t) => fmtr({ hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(t);
export const fmtDate = (t) => fmtr({ day: 'numeric', month: 'short', year: 'numeric' }).format(t);
export const fmtDay = (t) => fmtr({ weekday: 'short', day: 'numeric', month: 'short' }).format(t);
export const fmtDT = (t) => t == null ? '—' : fmtr({ weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }).format(t);
export const fmtDTs = (t) => t == null ? '—' : fmtr({ day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(t);
export const tzName = (t) => fmtr({ timeZoneName: 'short' }).formatToParts(t).find(p => p.type === 'timeZoneName')?.value || '';
export function localParts(t) {
  const p = Object.fromEntries(fmtr({ year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(t).map(x => [x.type, x.value]));
  return { y: +p.year, m: +p.month, d: +p.day, h: +p.hour, min: +p.minute, date: `${p.year}-${p.month}-${p.day}` };
}
export function ago(t, now = Date.now()) {
  const s = Math.round((now - t) / 1000);
  if (s < 0) return 'in ' + dur(-s * 1000);
  if (s < 45) return 'just now';
  return dur(s * 1000) + ' ago';
}
export function dur(msv) {
  if (msv == null) return '—';
  const s = Math.round(msv / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 120) return `${m} min`;
  const h = msv / 3600000;
  if (h < 72) return `${n(h, h < 10 ? 1 : 0)} h`;
  return `${n(h / 24, 1)} days`;
}
export function countdown(msv) {
  if (msv == null) return '—';
  const s = Math.max(0, Math.round(msv / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
  return m ? `${m}m ${String(ss).padStart(2, '0')}s` : `${ss}s`;
}

// ---------------------------------------------------------------- DOM

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
export const svgNS = 'http://www.w3.org/2000/svg';
export function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
export function clear(el) { while (el.firstChild) el.firstChild.remove(); return el; }
export function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
export function cssIn(el, name) { return getComputedStyle(el).getPropertyValue(name).trim(); }

export function toast(msg, ms_ = 3200) {
  const t = h('div', { class: 'toast', role: 'status', text: msg });
  document.body.append(t);
  setTimeout(() => t.remove(), ms_);
}

export function section(num, title, sub, ...kids) {
  const right = h('div', { class: 'right' });
  const sec = h('section', { class: 'section' },
    h('div', { class: 'kicker' }, h('h2', { text: title }), sub ? h('span', { class: 'sub', text: sub }) : null, right),
    ...kids);
  sec.right = right;
  return sec;
}

export function seg(options, value, onchange, label) {
  const wrap = h('div', { class: 'seg', role: 'group', 'aria-label': label || 'options' });
  for (const [v, text] of options) {
    const b = h('button', { type: 'button', 'aria-pressed': String(v === value), text });
    b.addEventListener('click', () => {
      for (const x of wrap.children) x.setAttribute('aria-pressed', 'false');
      b.setAttribute('aria-pressed', 'true');
      onchange(v);
    });
    wrap.append(b);
  }
  wrap.set = (v) => { [...wrap.children].forEach((b, i) => b.setAttribute('aria-pressed', String(options[i][0] === v))); };
  return wrap;
}

// ---------------------------------------------------------------- tooltip (one, shared)

const tipEl = () => document.getElementById('tip');
export function showTip(x, y, title, rows) {
  const t = tipEl();
  clear(t);
  if (title) t.append(h('div', { class: 't', text: title }));
  for (const r of rows) {
    const key = r.color ? h('i', { style: { background: r.color } }) : null;
    t.append(h('div', { class: 'r' }, h('span', {}, key, r.label || ''), h('b', { text: r.value ?? '' })));
  }
  t.hidden = false;
  const w = t.offsetWidth, hh = t.offsetHeight;
  let left = x + 16, top = y + 14;
  if (left + w > innerWidth - 8) left = x - w - 16;
  if (top + hh > innerHeight - 8) top = y - hh - 14;
  t.style.left = Math.max(8, left) + 'px';
  t.style.top = Math.max(8, top) + 'px';
}
export function hideTip() { const t = tipEl(); if (t) t.hidden = true; }

// attach delegated tooltips to an SVG/HTML container whose children carry data-tip='{"t":..,"rows":[..]}'
export function tips(container) {
  container.addEventListener('pointermove', (e) => {
    const el = e.target.closest('[data-tip]');
    if (!el) { hideTip(); return; }
    const d = JSON.parse(el.dataset.tip);
    showTip(e.clientX, e.clientY, d.t, d.rows || []);
  });
  container.addEventListener('pointerleave', hideTip);
}

// ---------------------------------------------------------------- canvas

export function hidpi(canvas, w, hgt) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(hgt * dpr);
  canvas.style.height = hgt + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}

export function observeWidth(el, cb) {
  let last = 0, raf = 0;
  const ro = new ResizeObserver((entries) => {
    const w = Math.round(entries[0].contentRect.width);
    if (w && w !== last) { last = w; cancelAnimationFrame(raf); raf = requestAnimationFrame(() => cb(w)); }
  });
  ro.observe(el);
  return () => ro.disconnect();
}

export function niceTicks(lo, hi, count = 5) {
  const span = hi - lo || 1;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(st => st >= step0) || mag * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

export function bandOf(dl, st) {
  if (dl == null) return 'failed';
  if (dl >= st.target_down) return 'target';
  if (dl < st.severe_down) return 'severe';
  if (dl < st.degraded_down) return 'degraded';
  return 'below';
}
export const BAND_LABEL = { target: 'On target', below: 'Below target', degraded: 'Degraded', severe: 'Severe', failed: 'Failed', outage: 'Outage', local: 'Pi/LAN fault' };
// Diverging speed colour: red arm below the degraded threshold (deepest at "severe"), neutral grey at it,
// teal arm up to the target. Used by the heatmap and calendar so slow periods stand out.
export function divColor(el, v, st) {
  if (v == null) return null;
  const g = (k) => getComputedStyle(el).getPropertyValue(k).trim();
  if (v >= st.degraded_down) {
    const f = Math.min(1, (v - st.degraded_down) / Math.max(1, st.target_down - st.degraded_down));
    return g(f >= 1 ? '--div-p3' : f >= 0.66 ? '--div-p2' : f >= 0.33 ? '--div-p1' : '--div-0');
  }
  const f = Math.min(1, (st.degraded_down - v) / Math.max(1, st.degraded_down - st.severe_down));
  return g(f >= 1 ? '--div-n3' : f >= 0.5 ? '--div-n2' : '--div-n1');
}
export const bandColor = (b) => ({ target: css('--dl'), below: css('--warn'), degraded: css('--serious'), severe: css('--crit'), failed: css('--crit'), outage: css('--crit'), local: css('--offline') }[b] || css('--mute'));

export const RANGES = [['1h', '1H'], ['6h', '6H'], ['24h', '24H'], ['7d', '7D'], ['30d', '30D'], ['90d', '90D'], ['1y', '1Y'], ['all', 'ALL']];
export function rangeMs(key, bounds) {
  const now = Date.now();
  const H = 3600e3, D = 24 * H;
  const map = { '1h': H, '6h': 6 * H, '24h': D, '7d': 7 * D, '30d': 30 * D, '90d': 90 * D, '1y': 365 * D };
  if (key === 'all') {
    const first = Math.min(...[bounds?.first_ms, bounds?.legacy_first_ms, bounds?.probe_first_ms].filter(x => x != null));
    return [Number.isFinite(first) ? first - H : now - 30 * D, now + 60e3];
  }
  return [now - (map[key] || 7 * D), now + 60e3];
}

// Waits that aren't a test: the 90 s gap after any test, a pause, the daily cap. Their end isn't when the next test runs.
export const WAITS = new Set(['spacing', 'paused', 'cap']);

export function nextTest(a) {
  if (!a) return null;
  const sched = a.next_scheduled_ms ?? Infinity;
  if (WAITS.has(a.next_kind)) return a.next_kind === 'spacing' ? Math.max(a.next_test_ms ?? 0, sched) : sched;
  return Math.min(a.next_test_ms ?? Infinity, sched);
}
