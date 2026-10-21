// Router and app shell for /speedtest. Each view module exports mount(el, params) -> { destroy() }.
import { api, state, BASE, clear, h, toast } from './util.js';
import * as bus from './bus.js';

const VIEWS = {
  live: () => import('./views/live.js'),
  evidence: () => import('./views/evidence.js'),
  compare: () => import('./views/compare.js'),
  explorer: () => import('./views/explorer.js'),
  export: () => import('./views/export.js'),
  annotations: () => import('./views/notes.js'),
  integrity: () => import('./views/integrity.js'),
  settings: () => import('./views/settings.js'),
  day: () => import('./views/day.js'),
};

let current = null;

function parse() {
  const p = location.pathname.slice(BASE.length) || '/';
  if (p === '/' || p === '') return ['live', {}];
  let m = p.match(/^\/day\/(\d{4}-\d{2}-\d{2})$/);
  if (m) return ['day', { day: m[1] }];
  m = p.match(/^\/test\/([0-9a-f-]{36})$/);
  if (m) return ['explorer', { open: m[1] }];
  const name = p.slice(1).split('/')[0];
  if (name === 'integrity') return ['evidence', { ...Object.fromEntries(new URLSearchParams(location.search)), focus: 'integrity' }];
  return [VIEWS[name] ? name : 'live', Object.fromEntries(new URLSearchParams(location.search))];
}

let routed = null;                                   // path + query of the view on screen

let navigation = 0;                                 // only the latest navigation may draw (see route)

export async function route() {
  const mine = ++navigation;
  routed = location.pathname + location.search;
  const [name, params] = parse();
  for (const a of document.querySelectorAll('.tabs a')) {
    a.toggleAttribute('aria-current', a.dataset.tab === name || (name === 'day' && a.dataset.tab === 'live'));
    if (a.hasAttribute('aria-current')) a.setAttribute('aria-current', 'page');
  }
  current?.destroy?.();
  current = null;
  const view = document.getElementById('view');
  clear(view);
  // A second navigation can start while this one waits for its module or data (a double click, Back pressed
  // quickly). Without these checks both views would draw into the page, one below the other.
  const mod = await VIEWS[name]();
  if (mine !== navigation) return;
  const mounted = await mod.mount(view, params);
  if (mine !== navigation) { mounted?.destroy?.(); return; }
  current = mounted;
  document.title = `${({ live: 'Live', day: params.day })[name] || name[0].toUpperCase() + name.slice(1)} · Line Monitor`;
}

export function go(path) {
  history.pushState({}, '', BASE + path);
  route();
  window.scrollTo({ top: 0 });
}
window.__go = go;

document.addEventListener('click', (e) => {
  const a = e.target.closest('a[href]');
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey || a.target || a.hasAttribute('download')) return;
  const href = a.getAttribute('href');
  if (!href.startsWith(BASE + '/') || href.includes('/exports/') || href.startsWith(BASE + '/static/')) return;
  e.preventDefault();
  go(href.slice(BASE.length));
});
window.addEventListener('popstate', () => {
  if (location.pathname + location.search === routed) { dispatchEvent(new Event('sectionnav:hash')); return; }   // only the #section changed
  route();
});

async function boot() {
  try {
    state.boot = await api('/bootstrap');
  } catch (e) {
    clear(document.getElementById('view')).append(h('div', { class: 'page' }, h('p', { class: 'empty', text: 'Could not reach the server: ' + e.message })));
    return;
  }
  state.tz = state.boot.settings.timezone;
  state.settings = state.boot.settings;
  bus.start();
  const statusEl = document.getElementById('status');
  bus.subscribe((d) => bus.renderHeader(statusEl, d));
  setInterval(() => { const d = bus.latest(); if (d) bus.renderHeader(statusEl, d); }, 1000);
  await route();
}

export async function refreshSettings() {
  state.boot = await api('/bootstrap');
  state.settings = state.boot.settings;
  state.tz = state.settings.timezone;
}

window.addEventListener('unhandledrejection', (e) => { if (e.reason?.message && e.reason.message !== 'signed out') toast(e.reason.message); });
boot();
