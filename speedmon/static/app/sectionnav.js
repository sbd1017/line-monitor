// Section dock: a small floating bar at the bottom of the Live page for jumping between its major sections.
// It lives outside the dashboard's data flow: it only reads element positions, never touches charts or state.
//
//   const nav = sectionNav(page, [{ id: 'live', label: 'Live', el: heroEl }, ...]);   nav.destroy() on unmount
//
// * Active section: the last group whose first element has passed a reading line a little below the sticky
//   header (and the sticky period bar, when it's stuck). IntersectionObserver tells us when a group start crosses
//   that line; the bottom of the page counts as the last group. One line, so no flicker at boundaries.
// * The URL hash follows passive scrolling with history.replaceState (no new history entries); a click also
//   replaces it, so Back still leaves the page rather than stepping through sections. /speedtest/#reliability
//   opens (and refreshes) at that section, and keeps it lined up while the charts finish loading.
import { h } from './util.js';

const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

export function sectionNav(page, groups) {
  const header = document.querySelector('.top');
  const filters = page.querySelector(':scope > .filters');
  const links = new Map();
  const ink = h('span', { class: 'secnav-ink', 'aria-hidden': 'true' });
  const track = h('div', { class: 'secnav-track' });
  for (const g of groups) {
    g.el.id = g.id;
    g.el.setAttribute('tabindex', '-1');                       // focus target after a jump, for screen readers
    g.el.classList.add('secnav-target');
    const a = h('a', { href: '#' + g.id, text: g.label });
    a.addEventListener('click', (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;      // new tab etc.: let the browser do it
      e.preventDefault();
      jump(g.id, true);
    });
    links.set(g.id, a);
    track.append(a);
  }
  track.append(ink);
  const nav = h('nav', { class: 'secnav', 'aria-label': 'Sections of this page' }, track);
  page.classList.add('has-secnav');
  document.body.classList.add('has-secnav');
  if (header) header.after(nav); else document.body.append(nav);   // early in the Tab order; it still floats at the bottom

  // ---------------------------------------------------------------- geometry
  const filtersStick = () => filters && getComputedStyle(filters).position === 'sticky';
  const headerH = () => (header ? header.getBoundingClientRect().height : 0);
  const filtersH = () => (filtersStick() ? filters.getBoundingClientRect().height : 0);
  // Space taken by sticky chrome when a group's heading arrives at the top: the period bar only sticks for
  // content that comes after it.
  const chromeFor = (el) => headerH() + (filters && filtersStick() && (filters.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) ? filtersH() : 0);
  const line = () => { const top = headerH() + filtersH(); return top + Math.min(220, (innerHeight - top) * 0.3); };
  const targetY = (g) => (g === groups[0] ? 0 : Math.max(0, g.el.getBoundingClientRect().top + scrollY - chromeFor(g.el) - 10));
  const setMargins = () => { for (const g of groups) g.el.style.scrollMarginTop = (chromeFor(g.el) + 10) + 'px'; };

  // ---------------------------------------------------------------- active section
  let active = null, settling = 0;
  function setActive(id, fromScroll) {
    if (id === active) return;
    active = id;
    for (const [k, a] of links) {
      if (k === id) a.setAttribute('aria-current', 'location'); else a.removeAttribute('aria-current');
    }
    const a = links.get(id);
    const first = !ink.style.width;                               // place it on load; animate only when it moves
    if (first) ink.style.transition = 'none';
    ink.style.transform = `translateX(${a.offsetLeft}px)`;
    ink.style.width = Math.max(8, a.offsetWidth - 26) + 'px';      // under the label text (13px padding each side)
    if (first) { void ink.offsetWidth; ink.style.transition = ''; }
    // keep the active label visible in the strip (phones), without moving the page
    const left = a.offsetLeft - (track.clientWidth - a.offsetWidth) / 2;
    if (track.scrollWidth > track.clientWidth) track.scrollTo({ left, behavior: reduced() ? 'auto' : 'smooth' });
    if (fromScroll && !settling) {
      const url = location.pathname + location.search + (id === groups[0].id ? '' : '#' + id);
      if (url !== location.pathname + location.search + location.hash) history.replaceState(history.state, '', url);
    }
  }
  function compute() {
    if (settling) return;                                       // a jump is under way: ignore what it scrolls past
    const atEnd = innerHeight + scrollY >= document.documentElement.scrollHeight - 4;
    let id = groups[0].id;
    if (atEnd && scrollY > 0) id = groups[groups.length - 1].id;
    else {
      const y = line();
      for (const g of groups) if (g.el.getBoundingClientRect().top <= y) id = g.id;
    }
    setActive(id, true);
  }

  let io = null, endIo = null;
  const end = h('div', { class: 'secnav-end', 'aria-hidden': 'true' });
  page.append(end);
  function observe() {
    io?.disconnect();
    const bottom = Math.max(0, innerHeight - line());
    io = new IntersectionObserver(compute, { rootMargin: `0px 0px -${Math.round(bottom)}px 0px`, threshold: [0, 1] });
    for (const g of groups) io.observe(g.el);
    endIo?.disconnect();
    endIo = new IntersectionObserver(compute);                      // reaching the bottom of the page
    endIo.observe(end);
    setMargins();
  }

  // ---------------------------------------------------------------- jumping
  let stopAlign = null;
  function jump(id, smooth) {
    const g = groups.find(x => x.id === id);
    if (!g) return;
    stopAlign?.();
    settling = 1;
    setActive(id, false);
    const url = location.pathname + location.search + (g === groups[0] ? '' : '#' + id);
    history.replaceState(history.state, '', url);
    window.scrollTo({ top: targetY(g), behavior: smooth && !reduced() ? 'smooth' : 'auto' });
    g.el.focus({ preventScroll: true });
    // Let the smooth scroll finish before scrolling takes over the active state again. If it hasn't arrived by
    // then (a slow device, a throttled tab) and the reader hasn't taken over, finish the jump instantly.
    let moved = false;
    const mine = () => { moved = true; };
    const evs = ['wheel', 'touchstart', 'keydown'];
    for (const ev of evs) addEventListener(ev, mine, { capture: true, passive: true, once: true });
    const done = () => {
      for (const ev of evs) removeEventListener(ev, mine, true);
      if (!moved && Math.abs(scrollY - targetY(g)) > 4) window.scrollTo({ top: targetY(g), behavior: 'auto' });
      settling = 0; active = null; compute();
    };
    if (smooth && !reduced()) {
      let t = setTimeout(done, 1500);
      if ('onscrollend' in window) addEventListener('scrollend', () => { clearTimeout(t); t = setTimeout(done, 80); }, { once: true });
    } else setTimeout(done, 50);
  }

  // Opening /speedtest/#section: the charts are still loading and the page keeps growing, so stay aligned to the
  // section for a few seconds unless the reader starts scrolling themselves.
  function alignToHash() {
    const id = decodeURIComponent(location.hash.slice(1));
    const g = groups.find(x => x.id === id);
    if (!g) return;
    const ro = new ResizeObserver(() => window.scrollTo({ top: targetY(g), behavior: 'auto' }));
    const stop = () => { ro.disconnect(); clearTimeout(t); for (const ev of ['wheel', 'touchstart', 'keydown', 'pointerdown']) removeEventListener(ev, stop, true); stopAlign = null; };
    for (const ev of ['wheel', 'touchstart', 'keydown', 'pointerdown']) addEventListener(ev, stop, { capture: true, passive: true });
    const t = setTimeout(stop, 4000);
    ro.observe(page);
    stopAlign = stop;
    jump(id, false);
  }

  // Hash changed without a page change (typed in the address bar, Back/Forward between hashes).
  const onHash = () => { if (groups.some(g => g.id === location.hash.slice(1))) jump(location.hash.slice(1), true); };
  let rz = 0;
  const onResize = () => { clearTimeout(rz); rz = setTimeout(() => { observe(); active = null; compute(); }, 120); };
  addEventListener('hashchange', onHash);
  addEventListener('sectionnav:hash', onHash);
  addEventListener('resize', onResize);

  observe();
  if (location.hash) requestAnimationFrame(alignToHash); else compute();

  return {
    destroy() {
      io?.disconnect();
      endIo?.disconnect();
      stopAlign?.();
      removeEventListener('hashchange', onHash);
      removeEventListener('sectionnav:hash', onHash);
      removeEventListener('resize', onResize);
      nav.remove();
      document.body.classList.remove('has-secnav');
    },
  };
}
