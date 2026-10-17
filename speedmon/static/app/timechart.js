// Zoomable, pannable time-series chart on canvas, with stacked panes sharing one time axis (small multiples:
// never two y-scales on one plot). Data comes from the server at the level of detail for the visible range:
// raw tests when few enough, otherwise buckets (mean line + min–max envelope). Zooming in re-fetches finer data.
// Controls: drag to pan · ⌘/Ctrl + scroll or trackpad pinch to zoom · double-click to zoom in · touch pinch.

import { css, cssIn, h, hidpi, observeWidth, niceTicks, showTip, hideTip, localParts, fmtTime, fmtDay, fmtDT, n, BAND_LABEL, bandOf, state } from './util.js';

const PAD_L = 46, PAD_R = 14, AXIS_H = 26, GAP = 14;
const MIN_SPAN = 20 * 60e3;

function tzOffset(t) {
  const p = localParts(t);
  return Date.UTC(p.y, p.m - 1, p.d, p.h, p.min) - Math.floor(t / 60000) * 60000;
}
function localMidnight(y, m, d) {
  const guess = Date.UTC(y, m - 1, d);
  let t = guess - tzOffset(guess);
  t = guess - tzOffset(t);
  return t;
}

const STEPS = [
  [60e3, 'm'], [5 * 60e3, 'm'], [15 * 60e3, 'm'], [30 * 60e3, 'm'], [3600e3, 'h'], [3 * 3600e3, 'h'], [6 * 3600e3, 'h'],
  [12 * 3600e3, 'h'], [86400e3, 'd'], [2 * 86400e3, 'd'], [7 * 86400e3, 'w'], [30 * 86400e3, 'M'], [91 * 86400e3, 'Q'], [365 * 86400e3, 'Y'],
];
export function timeTicks(a, b, px) {
  const want = Math.max(2, Math.floor(px / 110));
  const span = b - a;
  const [step, unit] = STEPS.find(([st]) => span / st <= want) || STEPS[STEPS.length - 1];
  const out = [];
  if (step < 86400e3) {
    const off = step >= 3600e3 ? tzOffset(a) : 0;            // align hour ticks to local midnight, not UTC
    for (let t = Math.ceil((a + off) / step) * step - off; t <= b; t += step) out.push(t);
    return { ticks: out, unit, fmt: (t) => { const p = localParts(t); return (p.h === 0 && p.min === 0) ? fmtDay(t) : fmtTime(t); } };
  }
  const p0 = localParts(a);
  if (unit === 'd' || unit === 'w') {
    let t = localMidnight(p0.y, p0.m, p0.d);
    const days = step / 86400e3;
    let i = 0;
    while (t <= b && i < 2000) {
      if (t >= a && (unit !== 'w' || new Date(t + tzOffset(t)).getUTCDay() === 1)) out.push(t);
      const p = localParts(t + 36 * 3600e3);
      t = localMidnight(p.y, p.m, p.d);
      i++;
    }
    const thin = unit === 'w' ? out : out.filter((_, k) => k % days === 0);
    return { ticks: thin, unit, fmt: (t) => fmtDay(t) };
  }
  const months = unit === 'M' ? 1 : unit === 'Q' ? 3 : 12;
  let y = p0.y, m = p0.m;
  const every = Math.max(months, Math.ceil(span / (want * 30.4 * 86400e3)));
  m = Math.floor((m - 1) / every) * every + 1;
  for (let i = 0; i < 600; i++) {
    const t = localMidnight(y, m, 1);
    if (t > b) break;
    if (t >= a) out.push(t);
    m += every;
    while (m > 12) { m -= 12; y++; }
  }
  return { ticks: out, unit, fmt: (t) => { const p = localParts(t); return p.m === 1 ? String(p.y) : new Intl.DateTimeFormat('en-GB', { timeZone: state.tz, month: 'short', year: '2-digit' }).format(t); } };
}

export class TimeChart {
  constructor(container, opts) {
    this.o = opts;
    this.el = container;
    this.canvas = h('canvas', { role: 'img', 'aria-label': opts.label || 'time series chart', tabindex: '0' });
    this.lod = h('span', { class: 'lod' });
    this.el.append(this.canvas);
    this.range = opts.range || [Date.now() - 7 * 86400e3, Date.now()];
    this.maxEnd = opts.range ? opts.range[1] : 0;          // a day view may end in the future (rest of today)
    this.data = null;
    this.hover = null;
    this.pointers = new Map();
    this.fetchSeq = 0;
    this.width = 0;
    this.height = opts.panes.reduce((a, p) => a + p.height, 0) + GAP * (opts.panes.length - 1) + AXIS_H + 8;
    this.stopObs = observeWidth(this.el, (w) => { this.width = w; this.draw(); });
    this.bind();
  }

  destroy() { this.stopObs(); clearTimeout(this.loadTimer); }

  setRange(a, b, refetch = true) {
    const span = Math.max(MIN_SPAN, b - a);
    const lo = this.o.limits ? this.o.limits()[0] : -Infinity;
    const hi = Math.max(Date.now() + 5 * 60e3, this.maxEnd || 0);
    let A = a, B = a + span;
    if (B > hi) { B = hi; A = B - span; }
    if (A < lo && B - A < hi - lo) { A = lo; B = A + span; }
    this.range = [Math.round(A), Math.round(B)];
    this.draw();
    if (refetch) { clearTimeout(this.loadTimer); this.loadTimer = setTimeout(() => this.load(), 160); }   // not this.t: that's the x→time method
    this.o.onRange?.(this.range);
  }

  async load() {
    const seq = ++this.fetchSeq;
    const [a, b] = this.range;
    const pad = (b - a) * 0.15;
    this.el.classList.add('loading');
    try {
      const d = await this.o.fetch(Math.round(a - pad), Math.round(b + pad), Math.max(300, this.width || 1000));
      if (seq !== this.fetchSeq) return;
      this.data = d;
      this.lod.textContent = d.mode === 'raw' ? `raw · ${n(d.n)} records` : `${bucketName(d.bucket_ms)} buckets · ${n(d.n)} records`;
      this.draw();
    } finally {
      if (seq === this.fetchSeq) this.el.classList.remove('loading');
    }
  }

  x(t) { const [a, b] = this.range; return PAD_L + (t - a) / (b - a) * (this.width - PAD_L - PAD_R); }
  t(x) { const [a, b] = this.range; return a + (x - PAD_L) / (this.width - PAD_L - PAD_R) * (b - a); }

  bind() {
    const c = this.canvas;
    c.addEventListener('wheel', (e) => {
      if (!(e.ctrlKey || e.metaKey) && Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;   // let the page scroll
      e.preventDefault();
      const r = c.getBoundingClientRect();
      const t0 = this.t(e.clientX - r.left);
      const [a, b] = this.range;
      if (e.ctrlKey || e.metaKey) {
        const k = Math.exp(e.deltaY * 0.012);
        this.setRange(t0 - (t0 - a) * k, t0 + (b - t0) * k);
      } else {
        const dt = e.deltaX / (this.width - PAD_L - PAD_R) * (b - a);
        this.setRange(a + dt, b + dt);
      }
    }, { passive: false });
    c.addEventListener('dblclick', (e) => {
      const r = c.getBoundingClientRect();
      const t0 = this.t(e.clientX - r.left);
      const [a, b] = this.range;
      const k = e.shiftKey ? 2 : 0.5;
      this.setRange(t0 - (t0 - a) * k, t0 + (b - t0) * k);
    });
    c.addEventListener('pointerdown', (e) => {
      c.setPointerCapture(e.pointerId);
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX, range0: [...this.range] });
      if (this.pointers.size === 2) {
        const [p1, p2] = [...this.pointers.values()];
        this.pinch = { d0: Math.abs(p1.x - p2.x) || 1, range0: [...this.range], mid: (p1.x + p2.x) / 2 };
      }
    });
    c.addEventListener('pointermove', (e) => {
      const r = c.getBoundingClientRect();
      const p = this.pointers.get(e.pointerId);
      if (p) {
        p.x = e.clientX; p.y = e.clientY;
        if (this.pointers.size === 2 && this.pinch) {
          const [p1, p2] = [...this.pointers.values()];
          const k = this.pinch.d0 / (Math.abs(p1.x - p2.x) || 1);
          const [a, b] = this.pinch.range0;
          const span = b - a;
          const tm = a + (this.pinch.mid - r.left - PAD_L) / (this.width - PAD_L - PAD_R) * span;
          this.setRange(tm - (tm - a) * k, tm + (b - tm) * k);
          return;
        }
        const dx = e.clientX - p.x0;
        if (Math.abs(dx) > 3) {
          this.dragging = true;
          const [a, b] = p.range0;
          const dt = -dx / (this.width - PAD_L - PAD_R) * (b - a);
          this.setRange(a + dt, b + dt);
          hideTip();
          return;
        }
      }
      this.hoverAt(e.clientX - r.left, e.clientY - r.top, e.clientX, e.clientY);
    });
    const up = (e) => {
      const p = this.pointers.get(e.pointerId);
      this.pointers.delete(e.pointerId);
      if (this.pointers.size < 2) this.pinch = null;
      if (p && !this.dragging && Math.abs(e.clientX - p.x0) < 4 && this.hover?.raw && this.o.onPick) this.o.onPick(this.hover.raw);
      this.dragging = false;
    };
    c.addEventListener('pointerup', up);
    c.addEventListener('pointercancel', up);
    c.addEventListener('pointerleave', () => { this.hover = null; hideTip(); this.draw(); });
    c.addEventListener('keydown', (e) => {
      const [a, b] = this.range, sp = b - a;
      if (e.key === 'ArrowLeft') this.setRange(a - sp * .2, b - sp * .2);
      else if (e.key === 'ArrowRight') this.setRange(a + sp * .2, b + sp * .2);
      else if (e.key === '+' || e.key === '=') this.setRange(a + sp * .25, b - sp * .25);
      else if (e.key === '-') this.setRange(a - sp * .5, b + sp * .5);
      else return;
      e.preventDefault();
    });
  }

  points() { return this.data ? this.data.points : []; }

  hoverAt(x, y, cx, cy) {
    const pts = this.points();
    if (!pts.length || x < PAD_L || x > this.width - PAD_R) { this.hover = null; hideTip(); this.draw(); return; }
    const t = this.t(x);
    let lo = 0, hi = pts.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (pts[mid][0] < t) lo = mid; else hi = mid; }
    const bw = this.data.bucket_ms || 0;
    const center = (p) => p[0] + bw / 2;
    const best = Math.abs(center(pts[lo]) - t) < Math.abs(center(pts[hi]) - t) ? pts[lo] : pts[hi];
    const px = this.x(center(best));
    if (Math.abs(px - x) > 40) { this.hover = null; hideTip(); this.draw(); return; }
    this.hover = { t: center(best), p: best, raw: this.data.mode === 'raw' ? best : null };
    this.draw();
    const rows = this.o.tooltip(best, this.data);
    const title = this.data.mode === 'raw' ? fmtDT(best[0]) : `${fmtDT(best[0])} · ${bucketName(bw)} bucket`;
    showTip(cx, cy, title, rows);
  }

  draw() {
    if (!this.width) return;
    const ctx = hidpi(this.canvas, this.width, this.height);
    const W = this.width;
    const [a, b] = this.range;
    const grid = cssIn(this.el, '--grid'), axis = cssIn(this.el, '--axis'), mute = cssIn(this.el, '--mute'), ink2 = cssIn(this.el, '--ink-2');
    const mono = cssIn(this.el, '--mono') || 'monospace';
    const pts = this.points();
    let top = 0;
    const ticks = timeTicks(a, b, W - PAD_L - PAD_R);
    for (const pane of this.o.panes) {
      const H = pane.height;
      const vis = pts.filter(p => p[0] >= a - (this.data?.bucket_ms || 0) && p[0] <= b);
      const vals = [];
      for (const p of vis) { const v = pane.max ? pane.max(p, this.data) : pane.value(p, this.data); if (v != null) vals.push(v); }
      const ths = (pane.thresholdsFrom ? pane.thresholdsFrom(this.data) : null) || pane.thresholds || [];
      let vmax = Math.max(pane.floor || 0, ...vals, ...ths.filter(th => th.show !== false && th.v != null).map(th => th.v * 1.05));
      if (!Number.isFinite(vmax) || vmax <= 0) vmax = pane.floor || 1;
      const yt = niceTicks(0, vmax, Math.max(2, Math.floor(H / 34)));
      const ymax = yt[yt.length - 1] < vmax ? vmax * 1.08 : yt[yt.length - 1];
      const y = (v) => top + 6 + (H - 6) * (1 - v / ymax);
      ctx.save();
      ctx.font = `10px ${mono}`;
      ctx.textBaseline = 'middle';
      for (const v of yt) {
        const yy = Math.round(y(v)) + .5;
        ctx.strokeStyle = grid; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(PAD_L, yy); ctx.lineTo(W - PAD_R, yy); ctx.stroke();
        ctx.fillStyle = mute; ctx.textAlign = 'right';
        ctx.fillText(v >= 1000 ? `${v / 1000}k` : String(v), PAD_L - 8, yy);
      }
      ctx.fillStyle = ink2; ctx.textAlign = 'left'; ctx.font = `600 10px ${mono}`;
      ctx.fillText(pane.label.toUpperCase(), PAD_L + 2, top + 10);
      ctx.beginPath(); ctx.rect(PAD_L, top, W - PAD_L - PAD_R, H + 2); ctx.clip();
      // annotations
      for (const an of (this.data?.annotations || [])) {
        const x1 = this.x(an.start_ms), x2 = an.end_ms ? this.x(an.end_ms) : x1;
        ctx.fillStyle = cssIn(this.el, '--glow-soft');
        if (x2 - x1 > 2) ctx.fillRect(x1, top, x2 - x1, H);
        ctx.strokeStyle = cssIn(this.el, '--glow'); ctx.globalAlpha = .55; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(Math.round(x1) + .5, top); ctx.lineTo(Math.round(x1) + .5, top + H); ctx.stroke();
        ctx.globalAlpha = 1;
      }
      for (const th of ths) {
        if (th.show === false || th.v == null) continue;
        const yy = Math.round(y(th.v)) + .5;
        const x0 = th.start != null ? Math.max(PAD_L, this.x(th.start)) : PAD_L;
        const x1 = th.end != null ? Math.min(W - PAD_R, this.x(th.end)) : W - PAD_R;
        if (x1 <= x0) continue;
        ctx.strokeStyle = th.color; ctx.lineWidth = 1; ctx.setLineDash([3, 4]); ctx.globalAlpha = .8;
        ctx.beginPath(); ctx.moveTo(x0, yy); ctx.lineTo(x1, yy); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
        if (x1 - x0 > 90) {
          ctx.fillStyle = mute; ctx.textAlign = 'right'; ctx.font = `10px ${mono}`;
          ctx.fillText(`${th.label} ${th.v}`, x1 - 4, yy - 7);
        }
      }
      pane.render(ctx, { pts: vis, x: (t) => this.x(t), y, top, H, data: this.data, el: this.el, hover: this.hover, range: this.range });
      if (this.hover) {
        const hx = Math.round(this.x(this.hover.t)) + .5;
        ctx.strokeStyle = cssIn(this.el, '--ink-2'); ctx.globalAlpha = .5; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(hx, top); ctx.lineTo(hx, top + H); ctx.stroke(); ctx.globalAlpha = 1;
      }
      ctx.restore();
      top += H + GAP;
    }
    // time axis
    const ay = top - GAP + 4;
    ctx.strokeStyle = axis; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD_L, ay + .5); ctx.lineTo(W - PAD_R, ay + .5); ctx.stroke();
    ctx.fillStyle = mute; ctx.font = `10px ${mono}`; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    let lastX = -1e9;
    for (const t of ticks.ticks) {
      const xx = this.x(t);
      const label = ticks.fmt(t), half = ctx.measureText(label).width / 2;
      if (xx - half < 2 || xx + half > W - 2 || xx - lastX < half + 40) continue;
      ctx.fillText(label, xx, ay + 8);
      ctx.beginPath(); ctx.moveTo(Math.round(xx) + .5, ay); ctx.lineTo(Math.round(xx) + .5, ay + 4); ctx.stroke();
      lastX = xx;
    }
    if (!pts.length && this.data) {
      ctx.fillStyle = mute; ctx.textAlign = 'center'; ctx.font = `12px ${cssIn(this.el, '--sans')}`;
      ctx.fillText('No measurements in this range', W / 2, this.height / 2 - 10);
    }
  }
}

export function bucketName(b) {
  if (!b) return 'raw';
  const m = b / 60e3;
  if (m < 60) return `${m}-min`;
  if (m < 1440) return `${m / 60}-hour`;
  if (m === 1440) return 'daily';
  if (m === 10080) return 'weekly';
  if (m === 43200) return '30-day';
  return `${m / 1440}-day`;
}

// ---------------------------------------------------------------- pane renderers

const gapBreak = (pts, maxGap) => {
  const segs = [];
  let cur = [];
  for (let i = 0; i < pts.length; i++) {
    if (cur.length && pts[i][0] - cur[cur.length - 1][0] > maxGap) { segs.push(cur); cur = []; }
    cur.push(pts[i]);
  }
  if (cur.length) segs.push(cur);
  return segs;
};

export function speedPane(key, label, colorVar, st, height = 220) {
  const F = (d) => Object.fromEntries(d.fields.map((f, i) => [f, i]));
  return {
    label, height, floor: key === 'dl' ? 100 : 20,
    thresholds: key === 'dl'
      ? [{ v: st.target_down, label: 'target', color: css('--good') }, { v: st.degraded_down, label: 'degraded', color: css('--serious') }]
      : [{ v: st.target_up, label: 'target', color: css('--good') }],
    // each package period draws its own threshold lines (they step at an upgrade)
    thresholdsFrom: (d) => d?.packages?.length ? d.packages.flatMap(p => key === 'dl'
      ? [{ v: p.target_down, label: 'target', color: css('--good'), start: p.start_ms, end: p.end_ms },
         { v: p.degraded_down, label: 'degraded', color: css('--serious'), start: p.start_ms, end: p.end_ms }]
      : [{ v: p.target_up, label: 'target', color: css('--good'), start: p.start_ms, end: p.end_ms }]) : null,
    value: (p, d) => p[F(d)[key]],
    max: (p, d) => d.mode === 'raw' ? p[F(d)[key]] : p[F(d)[key + '_max']],
    render(ctx, g) {
      const d = g.data; if (!d) return;
      const f = F(d);
      const color = cssIn(g.el, colorVar);
      const pts = g.pts;
      if (d.mode === 'raw') {
        const ok = pts.filter(p => p[f.ok] && p[f[key]] != null);
        const spacing = ok.length > 2 ? median(ok.slice(1).map((p, i) => p[0] - ok[i][0])) : 3600e3;
        ctx.lineWidth = 1.5; ctx.strokeStyle = color; ctx.globalAlpha = .55; ctx.lineJoin = 'round';
        for (const seg of gapBreak(ok.filter(p => !p[f.legacy]), Math.max(spacing * 4, 3 * 3600e3))) {
          ctx.beginPath();
          seg.forEach((p, i) => { const X = g.x(p[0]), Y = g.y(p[f[key]]); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
          ctx.stroke();
        }
        ctx.globalAlpha = 1;
        const r = ok.length > 600 ? 1.6 : ok.length > 200 ? 2.4 : 3.4;
        const surface = cssIn(g.el, '--surface');
        for (const p of ok) {
          const X = g.x(p[0]), Y = g.y(p[f[key]]);
          ctx.beginPath(); ctx.arc(X, Y, r + 1.2, 0, 7); ctx.fillStyle = surface; ctx.fill();
          ctx.beginPath(); ctx.arc(X, Y, r, 0, 7);
          let c = color;
          if (key === 'dl' && !p[f.legacy]) { const bd = p[f.band] || bandOf(p[f.dl], st); c = bd === 'severe' ? css('--crit') : bd === 'degraded' ? css('--serious') : color; }
          if (p[f.legacy]) c = cssIn(g.el, '--faint');
          ctx.fillStyle = c; ctx.globalAlpha = p[f.excluded] ? .3 : 1; ctx.fill(); ctx.globalAlpha = 1;
          if (p[f.anomaly]) { ctx.beginPath(); ctx.arc(X, Y, r + 3.5, 0, 7); ctx.strokeStyle = css('--warn'); ctx.lineWidth = 1.2; ctx.stroke(); }
        }
        if (key === 'dl') {
          for (const p of pts.filter(q => !q[f.ok])) {
            const X = g.x(p[0]);
            ctx.fillStyle = (p[f.failure] === 'lan_fault' || p[f.failure] === 'link_down') ? css('--offline') : css('--crit');
            ctx.fillRect(X - 1.5, g.top + g.H - 12, 3, 12);
          }
        }
        if (g.hover?.raw) {
          const p = g.hover.raw;
          if (p[f.ok] && p[f[key]] != null) { ctx.beginPath(); ctx.arc(g.x(p[0]), g.y(p[f[key]]), r + 3, 0, 7); ctx.strokeStyle = cssIn(g.el, '--ink'); ctx.lineWidth = 2; ctx.stroke(); }
        }
      } else {
        const bw = d.bucket_ms;
        const all = pts.filter(p => p[f[key]] != null);
        const isLegacy = (p) => f.n_legacy !== undefined && p[f.n_legacy] * 2 > p[f.n];
        // legacy speedtest-cli buckets are drawn faint: a different, less reliable method
        for (const [seg, c] of [[all.filter(p => !isLegacy(p)), color], [all.filter(isLegacy), cssIn(g.el, '--faint')]]) {
          ctx.fillStyle = c; ctx.globalAlpha = .14;
          for (const s of gapBreak(seg, bw * 3)) {
            ctx.beginPath();
            s.forEach((p, i) => { const X = g.x(p[0] + bw / 2); i ? ctx.lineTo(X, g.y(p[f[key + '_max']])) : ctx.moveTo(X, g.y(p[f[key + '_max']])); });
            for (let i = s.length - 1; i >= 0; i--) ctx.lineTo(g.x(s[i][0] + bw / 2), g.y(s[i][f[key + '_min']]));
            ctx.closePath(); ctx.fill();
          }
          ctx.globalAlpha = 1; ctx.strokeStyle = c; ctx.lineWidth = 2; ctx.lineJoin = 'round';
          for (const s of gapBreak(seg, bw * 3)) {
            ctx.beginPath();
            s.forEach((p, i) => { const X = g.x(p[0] + bw / 2), Y = g.y(p[f[key]]); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
            ctx.stroke();
          }
        }
        if (key === 'dl') {
          for (const p of pts) {
            const fails = p[f.n] - p[f.n_ok];
            if (fails > 0) { const X = g.x(p[0] + bw / 2); ctx.fillStyle = css('--crit'); ctx.fillRect(X - 1.5, g.top + g.H - Math.min(14, 4 + fails * 2), 3, Math.min(14, 4 + fails * 2)); }
          }
        }
      }
    },
  };
}

export function latencyPane(st, height = 140) {
  return {
    label: 'Latency (ms) · external probes', height, floor: 20,
    thresholds: [{ v: st.latency_degraded_ms, label: 'poor', color: css('--serious') }],
    value: (p, d) => p[d.fields.indexOf('ext')],
    max: (p, d) => p[d.fields.indexOf(d.mode === 'raw' ? 'ext' : 'ext_max')],
    render(ctx, g) {
      const d = g.data; if (!d) return;
      const fi = (k) => d.fields.indexOf(k);
      const lat = cssIn(g.el, '--lat');
      const bw = d.bucket_ms || 0;
      const pts = g.pts.filter(p => p[fi('ext')] != null);
      if (d.mode === 'bucket') {
        ctx.fillStyle = lat; ctx.globalAlpha = .12;
        for (const s of gapBreak(pts, bw * 3)) {
          ctx.beginPath();
          s.forEach((p, i) => { const X = g.x(p[0] + bw / 2); i ? ctx.lineTo(X, g.y(p[fi('ext_max')])) : ctx.moveTo(X, g.y(p[fi('ext_max')])); });
          for (let i = s.length - 1; i >= 0; i--) ctx.lineTo(g.x(s[i][0] + bw / 2), g.y(s[i][fi('ext_min')]));
          ctx.closePath(); ctx.fill();
        }
        ctx.globalAlpha = 1;
      }
      ctx.strokeStyle = lat; ctx.lineWidth = d.mode === 'raw' ? 1.25 : 2; ctx.lineJoin = 'round';
      for (const s of gapBreak(pts, Math.max(bw * 3, 5 * 60e3))) {
        ctx.beginPath();
        s.forEach((p, i) => { const X = g.x(p[0] + bw / 2), Y = g.y(p[fi('ext')]); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
        ctx.stroke();
      }
      // outages / local faults as bars on the floor
      for (const p of g.pts) {
        const bad = d.mode === 'raw' ? (['wan_down', 'dns_fail'].includes(p[fi('state')]) ? 1 : 0) : p[fi('n_outage')];
        const loc = d.mode === 'raw' ? (['lan_fault', 'link_down'].includes(p[fi('state')]) ? 1 : 0) : p[fi('n_local')];
        if (bad || loc) {
          const X = g.x(p[0]), w = Math.max(2, g.x(p[0] + (bw || 60e3)) - X);
          ctx.fillStyle = bad ? css('--crit') : css('--offline');
          ctx.fillRect(X, g.top + g.H - 10, w, 10);
        }
      }
    },
  };
}

function median(a) { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); return s[s.length >> 1]; }

export function speedTooltip(st) {
  return (p, d) => {
    const f = Object.fromEntries(d.fields.map((k, i) => [k, i]));
    if (d.mode === 'raw') {
      if (!p[f.ok]) return [{ label: 'Failed', value: p[f.failure] || 'failed', color: css('--crit') }, { label: 'Kind', value: p[f.kind] }];
      const rows = [
        { label: 'Download', value: `${n(p[f.dl], 1)} Mbps`, color: css('--dl') },
        { label: 'Upload', value: `${n(p[f.ul], 1)} Mbps`, color: css('--ul') },
        { label: 'Ping', value: `${n(p[f.ping], 1)} ms`, color: css('--lat') },
        { label: 'Band', value: BAND_LABEL[p[f.band] || bandOf(p[f.dl], st)] },
        { label: 'Kind', value: p[f.kind] + (p[f.legacy] ? ' (legacy)' : '') },
      ];
      if (p[f.anomaly]) rows.push({ label: 'Anomaly', value: 'unusual for this hour', color: css('--warn') });
      if (p[f.excluded]) rows.push({ label: 'Excluded', value: 'by owner' });
      rows.push({ label: '', value: 'click for details' });
      return rows;
    }
    return [
      { label: 'Mean download', value: `${n(p[f.dl], 0)} Mbps`, color: css('--dl') },
      { label: 'Range', value: `${n(p[f.dl_min], 0)}–${n(p[f.dl_max], 0)}` },
      { label: 'Mean upload', value: `${n(p[f.ul], 1)} Mbps`, color: css('--ul') },
      { label: 'Tests', value: `${n(p[f.n])} (${n(p[f.n] - p[f.n_ok])} failed)` },
      { label: 'Below degraded', value: n(p[f.n_degraded]) },
      ...(p[f.n_legacy] ? [{ label: 'Legacy speedtest-cli', value: `${n(p[f.n_legacy])} of ${n(p[f.n])}` }] : []),
    ];
  };
}

export function latencyTooltip() {
  return (p, d) => {
    const fi = (k) => d.fields.indexOf(k);
    if (d.mode === 'raw') return [
      { label: 'External', value: p[fi('ext')] == null ? 'no reply' : `${n(p[fi('ext')], 1)} ms`, color: css('--lat') },
      { label: 'Router', value: p[fi('gw')] == null ? '—' : `${n(p[fi('gw')], 2)} ms` },
      { label: 'Loss', value: `${n(p[fi('loss')], 0)}%` }, { label: 'State', value: p[fi('state')] },
    ];
    return [
      { label: 'Mean external', value: `${n(p[fi('ext')], 1)} ms`, color: css('--lat') },
      { label: 'Range', value: `${n(p[fi('ext_min')], 1)}–${n(p[fi('ext_max')], 1)}` },
      { label: 'Probes', value: n(p[fi('n')]) }, { label: 'Outage probes', value: n(p[fi('n_outage')]) },
    ];
  };
}
