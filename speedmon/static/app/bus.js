// One poller for /api/live shared by the header status and the Live view: every 10 s, every second while a
// test is running, paused while the tab is hidden.
import { api, h, clear, ago, countdown, nextTest } from './util.js';

const subs = new Set();
let last = null, timer = 0, running = false;

export function subscribe(fn) { subs.add(fn); if (last) fn(last); return () => subs.delete(fn); }
export function latest() { return last; }

async function poll() {
  clearTimeout(timer);
  if (document.hidden) { timer = setTimeout(poll, 4000); return; }
  try {
    last = await api('/live');
    for (const fn of subs) { try { fn(last); } catch (e) { console.error(e); } }
  } catch (e) { /* network blip: keep the last state */ }
  const testing = last?.agent?.state === 'testing';
  timer = setTimeout(poll, testing ? 1000 : 10000);
}
export function kick() { poll(); }
export function start() { if (!running) { running = true; poll(); document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); }); } }

const HEALTH = {
  ok: ['good', 'Online'], latency: ['warn', 'High latency'], degraded: ['serious', 'Degraded'], outage: ['crit', 'Internet down'],
  local: ['serious', 'Pi/LAN fault'], monitor_offline: ['off', 'Monitor offline'], stale: ['off', 'No recent probe'], unknown: ['off', 'Waiting for data'],
};
export function healthPill(code) {
  const [cls, label] = HEALTH[code] || HEALTH.unknown;
  return h('span', { class: 'pill' }, h('i', { class: `dot ${cls}` }), label);
}

export function renderHeader(el, d) {
  clear(el);
  const a = d.agent;
  if (a?.state === 'testing') {
    el.append(h('span', { class: 'pill' }, h('i', { class: 'dot live' }), `Testing · ${a.phase || ''}`));
  } else {
    el.append(healthPill(d.health));
    if (a?.online && a.next_scheduled_ms && !a.paused) el.append(h('span', { class: 'muted', text: `next test in ${countdown(Math.max(0, nextTest(a) - Date.now()))}` }));
    if (a?.paused) el.append(h('span', { class: 'muted', text: 'paused' }));
  }
}
