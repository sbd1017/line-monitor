// Instruments and small charts. Every chart has a hover layer; values shown only in tooltips are also available
// in the Evidence view tables and exports.

import { css, cssIn, h, s, clear, hidpi, observeWidth, niceTicks, tips, showTip, hideTip, n, pct, fmtDT, fmtDay, fmtTime, state, divColor } from './util.js';

const esc = (o) => JSON.stringify(o);
// SVG charts render at their real width so text stays 11px on phones and wide screens alike.
const fitW = (el) => Math.max(320, Math.min(1000, Math.round(el.clientWidth || el.parentElement?.clientWidth || 720)));

// ---------------------------------------------------------------- gauge
// A segmented ring drawn straight onto the page (no card). The scale follows the package in force; filled
// segments take the colour of the band the value falls in, unfilled ones show the bands faintly. Idle: the centre
// shows the latest result as a share of the package and a slow light sweep runs along the lit segments. During a
// test: the live Mbps, the phase and a progress trace.

const SCALE_STEPS = [10, 20, 25, 50, 100, 150, 200, 250, 300, 400, 500, 600, 800, 1000, 1200, 1500, 2000, 2500, 3000, 5000, 10000];

export class Gauge {
  constructor(el) {
    this.el = el;
    this.canvas = h('canvas', { role: 'img', 'aria-label': 'Speed gauge' });
    el.append(this.canvas);
    this.value = 0; this.target = 0; this.live = false; this.phase = null; this.progress = null;
    this.pkg = null; this.info = {}; this.t0 = performance.now();
    this.reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.stop = observeWidth(el, (w) => { this.w = w; this.draw(performance.now()); });
    let last = 0;
    this.lastTick = performance.now();
    const loop = (now) => {
      this.raf = requestAnimationFrame(loop);
      const moving = Math.abs(this.target - this.value) > 0.05;
      if (moving || this.live || (!this.reduce && now - last > 33)) { last = now; this.tick(now); this.draw(now); }
    };
    this.raf = requestAnimationFrame(loop);
  }
  destroy() { cancelAnimationFrame(this.raf); this.stop(); }
  set(v, opts = {}) {
    this.target = Math.max(0, v ?? 0);
    Object.assign(this, opts);
    if (this.reduce || document.hidden) { this.value = this.target; this.draw(performance.now()); }
  }
  tick(now) {                                  // time-based easing: a throttled or background tab catches up at once
    const dt = Math.min(2000, now - this.lastTick);
    this.lastTick = now;
    this.value += (this.target - this.value) * (1 - Math.exp(-dt / (this.live ? 140 : 420)));
  }
  scaleMax() {
    const adv = this.pkg?.advertised_down || 1000;
    const want = Math.max(adv * 1.12, this.target * 1.05);
    return SCALE_STEPS.find(x => x >= want) || want;
  }
  band(v) {
    const p = this.pkg || {};
    if (v >= p.target_down) return '--dl';
    if (v < p.severe_down) return '--crit';
    if (v < p.degraded_down) return '--serious';
    return '--warn';
  }
  draw(now) {
    if (!this.w) return;
    const W = this.w, H = Math.round(Math.min(380, Math.max(260, W * 0.86)));
    const ctx = hidpi(this.canvas, W, H);
    const v = this.value, max = this.scaleMax();
    const mono = cssIn(this.el, '--mono'), disp = cssIn(this.el, '--display');
    const cx = W / 2, cy = H * 0.53;
    const R = Math.min(W / 2 - 46, H * 0.44);
    const a0 = Math.PI * (0.5 + 0.2222), sweep = Math.PI * 2 * 0.7222;          // 260° opening at the bottom
    const ang = (x) => a0 + sweep * Math.min(1, Math.max(0, x / max));
    const N = Math.max(48, Math.min(84, Math.round(R / 2.2)));
    const r1 = R * 0.84, r2 = R;
    // light sweep position (idle only), 0..1 along the lit part, every ~5 s
    const cycle = ((now - this.t0) % 5200) / 5200;
    const sweepPos = this.live || this.reduce ? -1 : cycle * 1.4 - 0.2;
    const lit = v / max;
    const valueCol = cssIn(this.el, this.live ? '--glow' : this.band(this.target));
    ctx.lineCap = 'round';
    for (let i = 0; i < N; i++) {
      const f = (i + 0.5) / N, val = f * max, A = a0 + sweep * f;
      const on = f <= lit;
      // lit arc: one colour (the result's band), brightening towards the head; unlit: the zones, faintly
      const col = on ? valueCol : cssIn(this.el, this.band(val));
      const cos = Math.cos(A), sin = Math.sin(A);
      let alpha = on ? 0.35 + 0.65 * Math.pow(f / Math.max(lit, 0.001), 1.6) : 0.12, len = 1;
      if (on && sweepPos >= 0) {
        const d = Math.abs(f / Math.max(lit, 0.001) - sweepPos);
        if (d < 0.12) alpha = 1, len = 1 + (0.12 - d) * 0.9;
      }
      ctx.strokeStyle = col; ctx.globalAlpha = alpha; ctx.lineWidth = Math.max(2, (R * 2 * Math.PI * 0.7222) / N * 0.42);
      if (on && (f > lit - 2.5 / N || this.live)) { ctx.shadowColor = col; ctx.shadowBlur = this.live ? 16 : 10; } else ctx.shadowBlur = 0;
      ctx.beginPath(); ctx.moveTo(cx + cos * r1, cy + sin * r1); ctx.lineTo(cx + cos * (r1 + (r2 - r1) * len), cy + sin * (r1 + (r2 - r1) * len)); ctx.stroke();
    }
    ctx.shadowBlur = 0; ctx.globalAlpha = 1;
    // inner hairline + progress trace while testing
    const ri = R * 0.74;
    ctx.strokeStyle = cssIn(this.el, '--line-2'); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, ri, a0, a0 + sweep); ctx.stroke();
    if (this.live && this.progress != null) {
      ctx.strokeStyle = cssIn(this.el, '--glow'); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(cx, cy, ri, a0, a0 + sweep * Math.min(1, this.progress)); ctx.stroke();
    }
    // scale labels: 0, target, max (outside the ring, kept inside the canvas)
    const label = (x, text, colorVar, weight = '500') => {
      const A = ang(x), rr = R + 16;
      ctx.font = `${weight} 10px ${mono}`;
      const tw = ctx.measureText(text).width;
      let lx = cx + Math.cos(A) * rr, ly = cy + Math.sin(A) * rr;
      const ax = Math.cos(A) < -0.2 ? 'right' : Math.cos(A) > 0.2 ? 'left' : 'center';
      if (ax === 'right') lx = Math.max(lx, tw + 4); if (ax === 'left') lx = Math.min(lx, W - tw - 4);
      ctx.textAlign = ax; ctx.textBaseline = 'middle'; ctx.fillStyle = cssIn(this.el, colorVar);
      ctx.fillText(text, lx, ly);
    };
    label(0, '0', '--faint');
    label(max, String(max), '--faint');
    if (this.pkg) {
      label(this.pkg.target_down, `TARGET ${fmtN(this.pkg.target_down)}`, '--ink-2', '600');
      const A = ang(this.pkg.target_down);
      ctx.strokeStyle = cssIn(this.el, '--ink'); ctx.lineWidth = 2; ctx.lineCap = 'butt';
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(A) * (r2 + 3), cy + Math.sin(A) * (r2 + 3)); ctx.lineTo(cx + Math.cos(A) * (r2 + 9), cy + Math.sin(A) * (r2 + 9)); ctx.stroke();
    }
    ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    const big = Math.round(ri * 0.62);
    if (this.live) {
      ctx.fillStyle = cssIn(this.el, '--ink'); ctx.font = `650 ${big}px ${disp}`;
      ctx.fillText(n(v, v >= 100 ? 0 : 1), cx, cy + big * 0.32);
      ctx.font = `600 10px ${mono}`; ctx.fillStyle = cssIn(this.el, '--glow');
      ctx.fillText(`${(this.phase || 'testing').toUpperCase()}${this.progress != null ? ' · ' + Math.round(this.progress * 100) + '%' : ''}`, cx, cy + big * 0.32 + 22);
      ctx.fillStyle = cssIn(this.el, '--mute'); ctx.fillText('MBPS · LIVE', cx, cy - big * 0.62);
    } else if (this.info.pct != null) {
      ctx.fillStyle = cssIn(this.el, '--ink'); ctx.font = `650 ${big}px ${disp}`;
      const t = n(this.info.pct, 0);
      ctx.fillText(t, cx - big * 0.12, cy + big * 0.32);
      const tw = ctx.measureText(t).width;
      ctx.font = `600 ${Math.round(big * 0.34)}px ${disp}`; ctx.fillStyle = cssIn(this.el, '--mute'); ctx.textAlign = 'left';
      ctx.fillText('%', cx - big * 0.12 + tw / 2 + 3, cy + big * 0.32);
      ctx.textAlign = 'center'; ctx.font = `600 10px ${mono}`; ctx.fillStyle = cssIn(this.el, '--mute');
      ctx.fillText('OF ADVERTISED', cx, cy - big * 0.62);
      ctx.fillStyle = cssIn(this.el, '--ink-2');
      ctx.fillText(`${n(this.info.mbps, 1)} OF ${fmtN(this.pkg?.advertised_down)} MBPS ADVERTISED`, cx, cy + big * 0.32 + 22);
      if (this.info.when) { ctx.fillStyle = cssIn(this.el, '--faint'); ctx.fillText(this.info.when, cx, cy + big * 0.32 + 38); }
    } else {
      ctx.fillStyle = cssIn(this.el, '--mute'); ctx.font = `600 11px ${mono}`;
      ctx.fillText('WAITING FOR THE FIRST TEST', cx, cy);
    }
  }
}
const fmtN = (x) => x == null ? '—' : n(x, x % 1 ? 1 : 0);

// ---------------------------------------------------------------- network pulse

export class Pulse {
  constructor(el) {
    this.el = el; this.points = []; this.span = 3 * 3600e3;
    this.canvas = h('canvas', { role: 'img', 'aria-label': 'Network pulse: external latency from the 1-minute probes, last 3 hours' });
    el.append(this.canvas);
    this.stop = observeWidth(el, (w) => { this.w = w; });
    const loop = () => { this.raf = requestAnimationFrame(loop); this.draw(); };
    this.raf = requestAnimationFrame(loop);
    this.canvas.addEventListener('pointermove', (e) => this.hover(e));
    this.canvas.addEventListener('pointerleave', () => { this.hx = null; hideTip(); });
  }
  destroy() { cancelAnimationFrame(this.raf); this.stop(); }
  set(points) { this.points = points || []; }
  hover(e) {
    const r = this.canvas.getBoundingClientRect();
    this.hx = e.clientX - r.left;
    const now = Date.now(), W = r.width;
    const t = now - this.span + (this.hx / W) * this.span;
    let best = null, bd = Infinity;
    for (const p of this.points) { const d = Math.abs(p[0] - t); if (d < bd) { bd = d; best = p; } }
    if (best && bd < 3 * 60e3) {
      showTip(e.clientX, e.clientY, fmtDT(best[0]), [
        { label: 'External', value: best[1] == null ? 'no reply' : `${n(best[1], 1)} ms`, color: css('--lat') },
        { label: 'Router', value: best[2] == null ? '—' : `${n(best[2], 2)} ms` },
        { label: 'Loss', value: `${n(best[3] ?? 0, 0)}%` }, { label: 'State', value: best[4] }]);
    } else hideTip();
  }
  draw() {
    if (!this.w) return;
    const W = this.w, H = 150;
    const ctx = hidpi(this.canvas, W, H);
    const now = Date.now(), a = now - this.span;
    const x = (t) => (t - a) / this.span * W;
    const vals = this.points.map(p => p[1]).filter(v => v != null);
    const top = Math.max(30, Math.min(200, Math.ceil((Math.max(...vals, 20) * 1.25) / 10) * 10));
    const y = (v) => H - 18 - (H - 34) * Math.min(1, v / top);
    ctx.strokeStyle = cssIn(this.el, '--grid'); ctx.lineWidth = 1;
    ctx.fillStyle = cssIn(this.el, '--mute'); ctx.font = `10px ${cssIn(this.el, '--mono')}`;
    for (const v of niceTicks(0, top, 3)) { const yy = Math.round(y(v)) + .5; ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(W, yy); ctx.stroke(); ctx.fillText(`${v} ms`, 6, yy - 4); }
    for (let t = Math.ceil(a / 1800e3) * 1800e3; t < now; t += 1800e3) { if (x(t) < W - 40) ctx.fillText(fmtTime(t), x(t) + 3, H - 4); }
    for (const p of this.points) {
      if (['wan_down', 'dns_fail', 'lan_fault', 'link_down'].includes(p[4])) {
        ctx.fillStyle = cssIn(this.el, ['wan_down', 'dns_fail'].includes(p[4]) ? '--crit' : '--offline'); ctx.globalAlpha = .35;
        ctx.fillRect(x(p[0]), 10, Math.max(2, W * 60e3 / this.span), H - 28); ctx.globalAlpha = 1;
      }
    }
    // router trace (faint) then external trace with glow
    const trace = (idx, color, width, glow) => {
      ctx.beginPath(); let started = false, last = null;
      for (const p of this.points) {
        if (p[idx] == null || (last && p[0] - last > 4 * 60e3)) { started = false; }
        if (p[idx] == null) { last = p[0]; continue; }
        const X = x(p[0]), Y = y(p[idx]);
        started ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); started = true; last = p[0];
      }
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = 'round'; ctx.shadowColor = color; ctx.shadowBlur = glow; ctx.stroke(); ctx.shadowBlur = 0;
    };
    trace(2, cssIn(this.el, '--faint'), 1, 0);
    trace(1, cssIn(this.el, '--lat'), 1.75, 8);
    // head: a beat travelling from the last probe to "now"
    const last = [...this.points].reverse().find(p => p[1] != null);
    if (last) {
      const X = x(last[0]), Y = y(last[1]);
      const phase = (now % 1600) / 1600;
      ctx.beginPath(); ctx.arc(X, Y, 3 + phase * 12, 0, 7); ctx.strokeStyle = cssIn(this.el, '--lat'); ctx.globalAlpha = 1 - phase; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.globalAlpha = 1; ctx.beginPath(); ctx.arc(X, Y, 3.5, 0, 7); ctx.fillStyle = cssIn(this.el, '--lat'); ctx.fill();
      ctx.strokeStyle = cssIn(this.el, '--faint'); ctx.setLineDash([2, 4]); ctx.beginPath(); ctx.moveTo(X, Y); ctx.lineTo(W, Y); ctx.stroke(); ctx.setLineDash([]);
    }
    if (this.hx != null) { ctx.strokeStyle = cssIn(this.el, '--ink-2'); ctx.globalAlpha = .4; ctx.beginPath(); ctx.moveTo(this.hx, 0); ctx.lineTo(this.hx, H); ctx.stroke(); ctx.globalAlpha = 1; }
  }
}

// ---------------------------------------------------------------- columns (hour of day / weekday)

export function columns(el, rows, { labelOf, valueKey = 'median', st, height = 220, highlightWorst = 3, unit = 'Mbps', tipRows }) {
  clear(el);
  const W = fitW(el), H = height, L = 40, B = 24, T = 10, R = 8;
  const vals = rows.map(r => r[valueKey]).filter(v => v != null);
  const vmax = Math.max(st.target_down * 1.05, ...vals, 1);
  const yt = niceTicks(0, vmax, 4);
  const ymax = Math.max(yt[yt.length - 1], vmax);
  const y = (v) => T + (H - T - B) * (1 - v / ymax);
  const bw = (W - L - R) / rows.length;
  const svg = s('svg', { class: 'viz', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'bar chart' });
  const g = s('g', { class: 'grid' });
  for (const v of yt) { g.append(s('line', { x1: L, x2: W - R, y1: y(v), y2: y(v) })); svg.append(s('text', { x: L - 6, y: y(v) + 4, 'text-anchor': 'end' }, String(v))); }
  svg.prepend(g);
  const worst = [...rows].filter(r => r[valueKey] != null && (r.n_ok ?? r.n) >= 5).sort((a, b) => a[valueKey] - b[valueKey]).slice(0, highlightWorst).map(r => r);
  rows.forEach((r, i) => {
    const v = r[valueKey];
    const x = L + i * bw + bw * 0.18, w = Math.min(24, bw * 0.64);
    const xx = L + i * bw + (bw - w) / 2;
    if (v != null) {
      const hgt = Math.max(1, (H - B) - y(v));
      const isWorst = worst.includes(r);
      const color = v < st.severe_down ? css('--crit') : v < st.degraded_down ? css('--serious') : css('--dl');
      const r4 = Math.min(4, w / 2, hgt);
      const path = `M${xx},${H - B} V${y(v) + r4} Q${xx},${y(v)} ${xx + r4},${y(v)} H${xx + w - r4} Q${xx + w},${y(v)} ${xx + w},${y(v) + r4} V${H - B} Z`;
      svg.append(s('path', { d: path, fill: color, opacity: isWorst || !highlightWorst ? 1 : .78,
        'data-tip': esc({ t: labelOf(r, i), rows: tipRows ? tipRows(r) : [{ label: 'Median', value: `${n(v, 0)} ${unit}` }, { label: 'Tests', value: n(r.n_ok ?? r.n) }] }) }));
      if (r.p10 != null) svg.append(s('line', { x1: xx - 2, x2: xx + w + 2, y1: y(r.p10), y2: y(r.p10), stroke: css('--ink-2'), 'stroke-width': 1.5, opacity: .7 }));
    }
    const lab = labelOf(r, i);
    if (rows.length <= 12 || i % 3 === 0) svg.append(s('text', { x: L + i * bw + bw / 2, y: H - 7, 'text-anchor': 'middle' }, lab.length > 5 ? lab.slice(0, 3) : lab));
  });
  svg.append(s('line', { x1: L, x2: W - R, y1: y(st.target_down), y2: y(st.target_down), stroke: css('--good'), 'stroke-dasharray': '3 4' }));
  svg.append(s('text', { x: W - R, y: y(st.target_down) - 5, 'text-anchor': 'end' }, `target ${st.target_down}`));
  el.append(svg);
  tips(svg);
}

// ---------------------------------------------------------------- histogram

export function histogram(el, bins, st, { color = '--dl', unit = 'Mbps', target } = {}) {
  clear(el);
  if (!bins?.length) { el.append(h('div', { class: 'empty', text: 'No successful tests in this range.' })); return; }
  const W = fitW(el), H = 210, L = 36, B = 24, T = 10, R = 8;
  const vmax = Math.max(...bins.map(b => b.pct), 1);
  const yt = niceTicks(0, vmax, 4);
  const ymax = Math.max(yt[yt.length - 1], vmax);
  const top = bins[bins.length - 1].hi;
  const x = (v) => L + (W - L - R) * v / top;
  const y = (v) => T + (H - T - B) * (1 - v / ymax);
  const svg = s('svg', { class: 'viz', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'distribution' });
  const g = s('g', { class: 'grid' });
  for (const v of yt) { g.append(s('line', { x1: L, x2: W - R, y1: y(v), y2: y(v) })); svg.append(s('text', { x: L - 6, y: y(v) + 4, 'text-anchor': 'end' }, `${v}%`)); }
  svg.append(g);
  for (const b of bins) {
    if (!b.n) continue;
    const x0 = x(b.lo) + 1, x1 = x(b.hi) - 1;
    svg.append(s('rect', { x: x0, y: y(b.pct), width: Math.max(1, x1 - x0), height: Math.max(1, H - B - y(b.pct)), rx: 2, fill: cssIn(el, color),
      'data-tip': esc({ t: `${n(b.lo, 0)}–${n(b.hi, 0)} ${unit}`, rows: [{ label: 'Share of tests', value: pct(b.pct) }, { label: 'Tests', value: n(b.n) }] }) }));
  }
  for (const v of niceTicks(0, top, 6)) svg.append(s('text', { x: x(v), y: H - 7, 'text-anchor': 'middle' }, String(v)));
  const tg = target ?? st.target_down;
  if (tg <= top) { svg.append(s('line', { x1: x(tg), x2: x(tg), y1: T, y2: H - B, stroke: css('--good'), 'stroke-dasharray': '3 4' })); svg.append(s('text', { x: x(tg) - 4, y: T + 10, 'text-anchor': 'end' }, `target ${tg}`)); }
  el.append(svg);
  tips(svg);
}

// ---------------------------------------------------------------- percentile strip

export function percentileStrip(el, d, st, unit = 'Mbps') {
  clear(el);
  if (!d || !d.n) { el.append(h('div', { class: 'empty', text: 'No data.' })); return; }
  const W = fitW(el), H = 92, L = 14, R = 14;
  const P = d.p;
  const lo = Math.max(0, Math.min(d.min, st.target_down) * 0.97), hi = Math.max(d.max, st.target_down) * 1.01;
  const x = (v) => L + (W - L - R) * (v - lo) / (hi - lo || 1);
  const svg = s('svg', { class: 'viz', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'percentiles' });
  const yb = 40;
  svg.append(s('line', { x1: x(d.min), x2: x(d.max), y1: yb, y2: yb, stroke: css('--axis'), 'stroke-width': 2 }));
  if (P['10'] != null) svg.append(s('rect', { x: x(P['10']), y: yb - 9, width: Math.max(1, x(P['90']) - x(P['10'])), height: 18, rx: 3, fill: cssIn(el, '--dl'), opacity: .25 }));
  if (P['25'] != null) svg.append(s('rect', { x: x(P['25']), y: yb - 9, width: Math.max(1, x(P['75']) - x(P['25'])), height: 18, rx: 3, fill: cssIn(el, '--dl'), opacity: .55 }));
  const marks = [['min', d.min], ['P1', P['1']], ['P5', P['5']], ['P10', P['10']], ['median', d.median], ['P90', P['90']], ['P95', P['95']], ['P99', P['99']], ['max', d.max]].filter(m => m[1] != null);
  const placed = { above: [], below: [] };
  marks.forEach(([lab, v]) => {
    const X = x(v);
    const major = lab === 'median';
    svg.append(s('line', { x1: X, x2: X, y1: yb - 13, y2: yb + 13, stroke: major ? css('--ink') : css('--ink-2'), 'stroke-width': major ? 2.5 : 1.25 }));
    svg.append(s('rect', { x: X - 8, y: yb - 16, width: 16, height: 32, fill: 'transparent', 'data-tip': esc({ t: lab, rows: [{ label: 'Value', value: `${n(v, 1)} ${unit}` }] }) }));
    const text = `${lab} ${n(v, v >= 100 ? 0 : 1)}`;
    const w = text.length * 6.6;
    for (const side of major ? ['above', 'below'] : ['below', 'above']) {
      if (placed[side].every(([a, b]) => X + w / 2 < a || X - w / 2 > b)) {
        placed[side].push([X - w / 2 - 4, X + w / 2 + 4]);
        svg.append(s('text', { x: X, y: side === 'above' ? yb - 20 : yb + 28, 'text-anchor': 'middle', fill: major ? css('--ink') : null }, text));
        break;
      }
    }
  });
  if (st.target_down >= lo && st.target_down <= hi) {
    svg.append(s('line', { x1: x(st.target_down), x2: x(st.target_down), y1: yb - 30, y2: yb + 34, stroke: css('--good'), 'stroke-dasharray': '3 4' }));
    svg.append(s('text', { x: x(st.target_down) + 4, y: H - 4 }, `target ${st.target_down}`));
  }
  el.append(svg);
  tips(svg);
}

// ---------------------------------------------------------------- heatmap (canvas: thousands of cells)

export const heatColor = divColor;

export class Heatmap {
  constructor(el, { onDay } = {}) {
    this.el = el; this.onDay = onDay;
    this.canvas = h('canvas', { role: 'img', 'aria-label': 'Heatmap of mean download speed by day and hour' });
    this.scale = h('div', { class: 'scale' });
    el.append(this.canvas, this.scale);
    this.stop = observeWidth(el, (w) => { this.w = w; this.draw(); });
    this.canvas.addEventListener('pointermove', (e) => this.hover(e));
    this.canvas.addEventListener('pointerleave', () => hideTip());
    this.canvas.addEventListener('click', (e) => { const c = this.cell(e); if (c && this.onDay) this.onDay(c.row.day); });
  }
  destroy() { this.stop(); }
  set(rows, st) { this.rows = rows || []; this.st = st; this.draw(); }
  geom() {
    const L = 76, T = 18, W = this.w;
    const cw = (W - L - 4) / 24;
    const ch = Math.max(4, Math.min(18, 640 / Math.max(1, this.rows.length)));
    return { L, T, cw, ch, H: T + ch * this.rows.length + 4 };
  }
  cell(e) {
    const r = this.canvas.getBoundingClientRect();
    const { L, T, cw, ch } = this.geom();
    const col = Math.floor((e.clientX - r.left - L) / cw), ri = Math.floor((e.clientY - r.top - T) / ch);
    if (col < 0 || col > 23 || ri < 0 || ri >= this.rows.length) return null;
    return { row: this.rows[ri], col, v: this.rows[ri].cells[col] };
  }
  hover(e) {
    const c = this.cell(e);
    if (!c) { hideTip(); return; }
    const [mean, cnt, fails] = c.v;
    showTip(e.clientX, e.clientY, `${c.row.day} · ${String(c.col).padStart(2, '0')}:00–${String(c.col).padStart(2, '0')}:59`, [
      { label: 'Mean download', value: mean == null ? 'no successful test' : `${n(mean, 0)} Mbps`, color: mean == null ? null : heatColor(this.el, mean, c.row.target_down ? c.row : this.st) },
      { label: 'Tests', value: n(cnt) }, ...(fails ? [{ label: 'Failed', value: n(fails), color: css('--crit') }] : []), { label: '', value: 'click to open the day' }]);
  }
  draw() {
    if (!this.w || !this.rows) return;
    const { L, T, cw, ch, H } = this.geom();
    const ctx = hidpi(this.canvas, this.w, H);
    const mono = cssIn(this.el, '--mono');
    ctx.font = `10px ${mono}`; ctx.fillStyle = cssIn(this.el, '--mute'); ctx.textAlign = 'center';
    for (let hr = 0; hr < 24; hr += 3) ctx.fillText(String(hr).padStart(2, '0'), L + hr * cw + cw / 2, 11);
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    const labelEvery = Math.ceil(12 / ch);
    this.rows.forEach((r, i) => {
      const y = T + i * ch;
      if (i % labelEvery === 0) ctx.fillText(r.day.slice(5) === r.day.slice(5) ? r.day : r.day, L - 8, y + ch / 2);
      for (let c = 0; c < 24; c++) {
        const [mean, cnt, fails] = r.cells[c];
        const col = heatColor(this.el, mean, r.target_down ? r : this.st);   // each day against its own package
        ctx.fillStyle = col || cssIn(this.el, '--surface-2');
        ctx.fillRect(L + c * cw + 1, y + (ch > 6 ? 1 : 0), cw - 2, ch - (ch > 6 ? 2 : 0));
        if (fails && ch >= 6) { ctx.fillStyle = css('--crit'); ctx.beginPath(); ctx.arc(L + c * cw + cw - 5, y + ch / 2, Math.min(2.5, ch / 4), 0, 7); ctx.fill(); }
      }
    });
    clear(this.scale);
    this.scale.append(h('span', { text: 'severe' }), h('span', { class: 'ramp' }), h('span', { text: 'on target · grey = degraded threshold of the package in force that day' }),
      h('span', { style: { marginLeft: '12px', display: 'inline-flex', alignItems: 'center', gap: '6px' } }, h('i', { class: 'dot crit' }), 'failed test'),
      h('span', { style: { marginLeft: '12px' }, text: 'blank = no test' }));
  }
}

// ---------------------------------------------------------------- calendar

export function calendarView(el, days, st, onDay) {
  clear(el);
  if (!days.length) { el.append(h('div', { class: 'empty', text: 'No days with data yet.' })); return; }
  const cls = { target: '--seq-6', below: '--seq-4', degraded: '--serious', severe: '--crit', failed: '--crit', none: '--surface-2' };
  const first = new Date(days[0].day + 'T12:00:00Z'), last = new Date(days[days.length - 1].day + 'T12:00:00Z');
  const start = new Date(first); start.setUTCDate(start.getUTCDate() - ((start.getUTCDay() + 6) % 7));
  const map = new Map(days.map(d => [d.day, d]));
  const weeks = Math.ceil((last - start) / (7 * 86400e3)) + 1;
  const C = 13, G = 3, L = 28, T = 18;
  const svg = s('svg', { width: L + weeks * (C + G) + 4, height: T + 7 * (C + G) + 4, role: 'img', 'aria-label': 'calendar of daily connection quality' });
  ['Mon', '', 'Wed', '', 'Fri', '', 'Sun'].forEach((d, i) => d && svg.append(s('text', { x: 0, y: T + i * (C + G) + 10, fill: css('--mute'), 'font-size': 10, 'font-family': css('--mono') }, d)));
  let lastMonth = -1, lastMonthW = null;
  for (let w = 0; w < weeks; w++) {
    for (let d = 0; d < 7; d++) {
      const dt = new Date(start.getTime() + (w * 7 + d) * 86400e3);
      if (dt > last) break;
      const key = dt.toISOString().slice(0, 10);
      const rec = map.get(key);
      if (d === 0 && dt.getUTCMonth() !== lastMonth && (lastMonthW === null || w - lastMonthW >= 3)) {
        lastMonth = dt.getUTCMonth(); lastMonthW = w;
        svg.append(s('text', { x: L + w * (C + G), y: 11, fill: css('--mute'), 'font-size': 10, 'font-family': css('--mono') }, dt.toLocaleString('en-GB', { month: 'short', timeZone: 'UTC' })));
      }
      const fill = rec?.median != null ? divColor(el, rec.median, rec.target_down ? rec : st) : css(rec?.class === 'failed' ? '--crit' : '--surface-2');
      const rect = s('rect', { x: L + w * (C + G), y: T + d * (C + G), width: C, height: C, rx: 2, fill });
      if (rec) {
        rect.setAttribute('data-day', key);
        rect.setAttribute('data-tip', esc({ t: fmtDay(new Date(key + 'T12:00:00Z')), rows: [
          { label: 'Median download', value: rec.median == null ? '—' : `${n(rec.median, 0)} Mbps` },
          { label: 'On target', value: pct(rec.pct_target, 0) }, { label: 'Tests', value: `${n(rec.n)} (${n(rec.failed)} failed)` },
          ...(rec.outage_min ? [{ label: 'Outage (probes)', value: `${n(rec.outage_min, 0)} min` }] : [])] }));
        if (rec.outage_min >= 2) svg.append(s('circle', { cx: L + w * (C + G) + C - 3, cy: T + d * (C + G) + 3, r: 2, fill: css('--crit'), 'pointer-events': 'none' }));
      }
      svg.prepend(rect);
    }
  }
  svg.addEventListener('click', (e) => { const r = e.target.closest('[data-day]'); if (r) onDay(r.dataset.day); });
  el.append(svg);
  tips(svg);
}

// ---------------------------------------------------------------- reliability timeline

const TL_CLASS = { ok: '--dl', latency: '--warn', outage: '--crit', local: '--serious', offline: '--offline', partial: '--offline', unknown: '--surface-2' };
export const TL_LABEL = { ok: 'Online', latency: 'High latency', outage: 'Internet outage', local: 'Pi/LAN fault', offline: 'Monitor offline', partial: 'Partly monitored', unknown: 'Unknown / no data' };

export class Timeline {
  constructor(el) {
    this.el = el;
    this.canvas = h('canvas', { role: 'img', 'aria-label': 'Reliability timeline' });
    el.append(this.canvas);
    this.stop = observeWidth(el, (w) => { this.w = w; this.draw(); });
    this.canvas.addEventListener('pointermove', (e) => this.hover(e));
    this.canvas.addEventListener('pointerleave', () => hideTip());
  }
  destroy() { this.stop(); }
  set(d) { this.d = d; this.draw(); }
  hover(e) {
    if (!this.d) return;
    const r = this.canvas.getBoundingClientRect();
    const { from_ms: a, to_ms: b } = this.d.range;
    const t = a + (e.clientX - r.left) / r.width * (b - a);
    const seg = this.d.segments.find(sg => sg[0] <= t && t < sg[1]);
    const marks = this.d.tests.filter(m => Math.abs(m[0] - t) < (b - a) / r.width * 3);
    const rows = [];
    if (seg) rows.push({ label: TL_LABEL[seg[2]] || seg[2], value: `${fmtTime(seg[0])}–${fmtTime(seg[1])}`, color: css(TL_CLASS[seg[2]]) });
    for (const m of marks.slice(0, 3)) rows.push({ label: 'Test ' + m[1], value: fmtTime(m[0]), color: css(m[1] === 'severe' || m[1] === 'failed' || m[1] === 'outage' ? '--crit' : '--serious') });
    if (rows.length) showTip(e.clientX, e.clientY, fmtDT(t), rows); else hideTip();
  }
  draw() {
    if (!this.w || !this.d) return;
    const W = this.w, H = 64;
    const ctx = hidpi(this.canvas, W, H);
    const { from_ms: a, to_ms: b } = this.d.range;
    const x = (t) => (t - a) / (b - a) * W;
    for (const [s0, s1, c] of this.d.segments) {
      ctx.fillStyle = cssIn(this.el, TL_CLASS[c] || '--surface-2');
      ctx.globalAlpha = c === 'ok' ? .55 : 1;
      ctx.fillRect(x(s0), 16, Math.max(1, x(s1) - x(s0)), 22);
    }
    ctx.globalAlpha = 1;
    for (const [t, band] of this.d.tests) {
      const col = cssIn(this.el, band === 'severe' || band === 'failed' || band === 'outage' ? '--crit' : band === 'degraded' ? '--serious' : band === 'local' ? '--offline' : '--warn');
      ctx.fillStyle = col; ctx.fillRect(x(t) - 1, 42, 2, band === 'below' ? 6 : 12);
    }
    ctx.fillStyle = cssIn(this.el, '--mute'); ctx.font = `10px ${cssIn(this.el, '--mono')}`;
    ctx.fillText('connectivity (probes)', 0, 10); ctx.fillText('speed tests below target', 0, 63);
  }
}

// ---------------------------------------------------------------- Connection DNA

export function dnaGlyph(canvas, d, st, size = 280) {
  size = Math.max(size || 280, 60);                // a hidden or tiny card must not give a negative radius
  const ctx = hidpi(canvas, size, size);
  const c = size / 2, R0 = size * 0.14, R1 = size * 0.46;
  const sp = d.spokes;
  const r = (v) => R0 + (R1 - R0) * Math.min(1, (v || 0) / Math.max(st.target_down, 1));
  const el = canvas;
  ctx.strokeStyle = cssIn(el, '--grid'); ctx.lineWidth = 1;
  for (const f of [0.5, 1]) { ctx.beginPath(); ctx.arc(c, c, R0 + (R1 - R0) * f, 0, 7); ctx.stroke(); }
  const ang = (hr) => -Math.PI / 2 + hr / 24 * Math.PI * 2;
  // p10-p90 band as a soft ribbon
  ctx.beginPath();
  sp.forEach((s_, i) => { const A = ang(i), rr = r(s_.p90 ?? s_.median); i ? ctx.lineTo(c + Math.cos(A) * rr, c + Math.sin(A) * rr) : ctx.moveTo(c + Math.cos(A) * rr, c + Math.sin(A) * rr); });
  for (let i = 23; i >= 0; i--) { const A = ang(i), rr = r(sp[i].p10 ?? sp[i].median); ctx.lineTo(c + Math.cos(A) * rr, c + Math.sin(A) * rr); }
  ctx.closePath(); ctx.fillStyle = cssIn(el, '--dl'); ctx.globalAlpha = .16; ctx.fill(); ctx.globalAlpha = 1;
  // spokes: median, jitter as wobble width, failures as red notch
  sp.forEach((s_, i) => {
    const A = ang(i);
    if (s_.median == null) { ctx.fillStyle = cssIn(el, '--faint'); ctx.beginPath(); ctx.arc(c + Math.cos(A) * R0, c + Math.sin(A) * R0, 1.5, 0, 7); ctx.fill(); return; }
    const rr = r(s_.median);
    const col = s_.median >= st.target_down ? cssIn(el, '--dl') : s_.median >= st.degraded_down ? cssIn(el, '--warn') : cssIn(el, '--crit');
    ctx.strokeStyle = col; ctx.lineWidth = 2 + Math.min(6, (s_.jitter || 0) * 1.2); ctx.lineCap = 'round'; ctx.globalAlpha = .9;
    ctx.beginPath(); ctx.moveTo(c + Math.cos(A) * R0, c + Math.sin(A) * R0); ctx.lineTo(c + Math.cos(A) * rr, c + Math.sin(A) * rr); ctx.stroke();
    ctx.globalAlpha = 1;
    if (s_.failed) { ctx.fillStyle = css('--crit'); ctx.beginPath(); ctx.arc(c + Math.cos(A) * (R1 + 7), c + Math.sin(A) * (R1 + 7), 2.5, 0, 7); ctx.fill(); }
  });
  // core: latency ring
  const ping = d.summary.ping;
  ctx.beginPath(); ctx.arc(c, c, R0 - 6, 0, 7); ctx.strokeStyle = cssIn(el, '--lat'); ctx.lineWidth = Math.max(1, Math.min(8, (ping || 0) / 4)); ctx.stroke();
  ctx.fillStyle = cssIn(el, '--ink'); ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.font = `650 ${Math.round(size * 0.075)}px ${cssIn(el, '--display')}`;
  ctx.fillText(d.summary.median == null ? '—' : n(d.summary.median, 0), c, c - 2);
  ctx.font = `9px ${cssIn(el, '--mono')}`; ctx.fillStyle = cssIn(el, '--mute');
  ctx.fillText('MBPS', c, c + size * 0.05);
  for (const hr of [0, 6, 12, 18]) { const A = ang(hr); ctx.fillText(String(hr).padStart(2, '0'), c + Math.cos(A) * (R1 + 16), c + Math.sin(A) * (R1 + 16)); }
}

// ---------------------------------------------------------------- simple daily line chart (SVG, for Evidence/Compare)

export function lineChart(el, series, { target, height = 230, xFmt = (x) => x, unit = 'Mbps', yMin = 0 } = {}) {
  clear(el);
  const all = series.flatMap(sr => sr.points.filter(p => p[1] != null));
  if (!all.length) { el.append(h('div', { class: 'empty', text: 'No data.' })); return; }
  const W = fitW(el), H = height, L = 44, R = 10, T = 12, B = 26;
  const xs = series[0].points.map(p => p[0]);
  const vmax = Math.max(...all.map(p => p[1]), target || 0) * 1.05;
  const yt = niceTicks(yMin, vmax, 4);
  const ymax = Math.max(yt[yt.length - 1], vmax);
  const x = (i) => L + (W - L - R) * (xs.length > 1 ? i / (xs.length - 1) : 0.5);
  const y = (v) => T + (H - T - B) * (1 - (v - yMin) / (ymax - yMin));
  const svg = s('svg', { class: 'viz', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': series.map(sr => sr.name).join(', ') });
  const g = s('g', { class: 'grid' });
  for (const v of yt) { g.append(s('line', { x1: L, x2: W - R, y1: y(v), y2: y(v) })); svg.append(s('text', { x: L - 6, y: y(v) + 4, 'text-anchor': 'end' }, String(v))); }
  svg.append(g);
  if (target) { svg.append(s('line', { x1: L, x2: W - R, y1: y(target), y2: y(target), stroke: css('--good'), 'stroke-dasharray': '3 4' })); svg.append(s('text', { x: W - R, y: y(target) - 5, 'text-anchor': 'end' }, `target ${target}`)); }
  for (const sr of series) {
    let d = '', pen = false;
    sr.points.forEach((p, i) => { if (p[1] == null) { pen = false; return; } d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`; pen = true; });
    svg.append(s('path', { d, fill: 'none', stroke: cssIn(el, sr.color), 'stroke-width': sr.width || 2, 'stroke-linejoin': 'round', 'stroke-dasharray': sr.dash || null, opacity: sr.opacity || 1 }));
  }
  const step = Math.max(1, Math.ceil(xs.length / 8));
  xs.forEach((xv, i) => { if (i % step === 0) svg.append(s('text', { x: x(i), y: H - 8, 'text-anchor': 'middle' }, xFmt(xv))); });
  const bw = (W - L - R) / Math.max(1, xs.length);
  xs.forEach((xv, i) => svg.append(s('rect', { x: x(i) - bw / 2, y: T, width: bw, height: H - T - B, fill: 'transparent',
    'data-tip': esc({ t: String(xFmt(xv, true)), rows: series.map(sr => ({ label: sr.name, value: sr.points[i][1] == null ? '—' : `${n(sr.points[i][1], 0)} ${unit}`, color: cssIn(el, sr.color) })) }) })));
  el.append(svg);
  tips(svg);
}
