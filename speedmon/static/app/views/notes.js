// Annotations: your own timeline notes ("Virgin engineer visit", "Hub rebooted"…). They never alter measurements.
import { api, state, h, clear, section, fmtDT, toast, localParts } from '../util.js';

const toLocalInput = (t) => { if (t == null) return ''; const p = localParts(t); return `${p.date}T${String(p.h).padStart(2, '0')}:${String(p.min).padStart(2, '0')}`; };

export async function mount(root) {
  const cats = state.boot.annotation_categories;
  const page = h('div', { class: 'page' });
  root.append(page);
  const form = { title: h('input', { type: 'text', maxlength: 120, placeholder: 'e.g. Virgin engineer visit' }), category: h('select', {}, ...cats.map(c => h('option', { value: c, text: c }))),
    start: h('input', { type: 'datetime-local' }), end: h('input', { type: 'datetime-local' }), body: h('textarea', { rows: 3, maxlength: 4000, placeholder: 'Notes (optional)' }) };
  form.start.value = toLocalInput(Date.now());
  let editing = null;
  const save = h('button', { class: 'btn primary', text: 'Add note', onclick: submit });
  const cancel = h('button', { class: 'btn', text: 'Cancel', style: { display: 'none' }, onclick: reset });
  page.append(section('01', 'Timeline notes', 'shown on the charts and in exports; times are your local time',
    h('div', { class: 'form-grid' }, h('label', { class: 'field' }, 'Title', form.title), h('label', { class: 'field' }, 'Category', form.category),
      h('label', { class: 'field' }, 'Start', form.start), h('label', { class: 'field' }, h('span', {}, 'End ', h('small', { text: '(optional, for a period)' })), form.end)),
    h('label', { class: 'field', style: { marginTop: '14px' } }, 'Notes', form.body), h('div', { style: { display: 'flex', gap: '8px', marginTop: '14px' } }, save, cancel)));
  const list = h('div', { class: 'list' });
  page.append(section('02', 'All notes', '', list));
  function reset() { editing = null; form.title.value = ''; form.body.value = ''; form.end.value = ''; form.start.value = toLocalInput(Date.now()); save.textContent = 'Add note'; cancel.style.display = 'none'; }
  async function submit() {
    const body = { title: form.title.value, category: form.category.value, start: form.start.value, end: form.end.value || null, body: form.body.value };
    try { await api(editing ? `/annotations/${editing}` : '/annotations', { body }); toast('Saved'); reset(); load(); } catch (e) { toast(e.message); }
  }
  async function load() {
    const r = await api('/annotations');
    clear(list);
    if (!r.items.length) list.append(h('p', { class: 'muted', text: 'No notes yet.' }));
    for (const a of r.items) {
      list.append(h('div', { class: 'item', style: { gridTemplateColumns: '170px 1fr auto' } },
        h('span', { class: 'mono', text: fmtDT(a.start_ms) + (a.end_ms ? ` →\n${fmtDT(a.end_ms)}` : '') }),
        h('span', {}, h('span', { class: 'tag', text: a.category }), ' ', h('b', { text: a.title }), a.body ? h('div', { class: 'muted', text: a.body }) : null),
        h('span', { style: { display: 'flex', gap: '6px' } },
          h('button', { class: 'btn small', text: 'Edit', onclick: () => { editing = a.uuid; form.title.value = a.title; form.category.value = a.category; form.body.value = a.body; form.start.value = toLocalInput(a.start_ms); form.end.value = toLocalInput(a.end_ms); save.textContent = 'Save changes'; cancel.style.display = ''; window.scrollTo({ top: 0, behavior: 'smooth' }); } }),
          h('button', { class: 'btn small danger', text: 'Delete', onclick: async () => { if (confirm(`Delete "${a.title}"? (Kept in the history log.)`)) { await api(`/annotations/${a.uuid}`, { body: { delete: true } }); load(); } } }))));
    }
  }
  await load();
  return {};
}
