/* ────────────────────────────────────────────────────────────────────────────
 * ACTIVITY LOGS — who signed in and what they changed, opened and downloaded (/api/audit, newest first).
 * Shown with the pager of the other pages (10 rows, 5 on a phone) over every matching row; rows come 500
 * at a time and the next 500 load when a page past them is opened. The client admin sees users and admins; the Trinetra team also
 * sees its own team's activity.
 * ──────────────────────────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);

const state = { range: null, role: '', category: '', q: '', items: [], next: null, counts: {}, seq: 0, more: false };
const logPager = pager('log-pager', () => render());
const ROLE = { user: 'User', admin: 'Admin', support: 'Trinetra team', anonymous: 'Not signed in', system: 'System' };
const CAT = { 'sign-in': 'Sign-in', account: 'Account', alerts: 'Alerts', whitelist: 'Whitelist', settings: 'Settings',
              data: 'Data', pipeline: 'DeepStream', recordings: 'Recordings', pages: 'Page', reports: 'Download', other: 'Other' };

function fmtTs(ts) {
  const d = new Date(ts);
  return `${escapeHtml(fmtDay(d))} · ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function render() {
  const total = Object.entries(state.counts).reduce((n, [, c]) => n + c, 0);
  $('log-count').textContent = total;
  const rows = logPager.slice(state.items, Math.max(total, state.items.length));
  // a page past the loaded rows: fetch the next (older) ones
  const waiting = state.next && logPager.page * logPager.size > state.items.length;
  if (waiting && !state.more) load(true);
  $('log-body').innerHTML = waiting && !rows.length ? '<tr><td colspan="5" class="empty">loading…</td></tr>'
    : rows.length ? rows.map(r => `<tr class="${r.ok ? '' : 'log-refused'}">
      <td class="nowrap">${fmtTs(r.ts)}</td>
      <td><div class="emp-cell-name">${escapeHtml(r.actor || '—')}</div><div class="emp-cell-sub">${escapeHtml(ROLE[r.actor_role] || r.actor_role)}</div></td>
      <td><span class="tag ${r.ok ? 'dept' : 'bad'}">${escapeHtml(CAT[r.category] || r.category)}</span> ${escapeHtml(r.action)}</td>
      <td>${escapeHtml(r.target || '—')}</td>
      <td class="nowrap muted">${escapeHtml(r.ip || '—')}</td></tr>`).join('')
    : `<tr><td colspan="5" class="empty">No activity ${state.q ? 'matches' : 'in this period'}.</td></tr>`;
}

async function load(more = false) {
  const seq = ++state.seq;
  if (!more) logPager.reset();
  state.more = more;
  const q = new URLSearchParams({ date_from: state.range.from, date_to: state.range.to, limit: 500 });
  if (state.role) q.set('role', state.role);
  if (state.category) q.set('category', state.category);
  if (state.q) q.set('q', state.q);
  if (more && state.next) q.set('before', state.next);
  try {
    const d = await apiGet(`/api/audit?${q}`);
    if (seq !== state.seq) return;                     // a newer filter is already loading
    state.items = more ? state.items.concat(d.items) : d.items;
    state.next = d.next;
    state.counts = d.counts;
    render();
  } catch (err) {
    toast(`Could not load the activity log: ${err.message}`);
  } finally {
    if (seq === state.seq) state.more = false;
  }
}

let _qTimer;
function init() {
  initSearch({
    placeholder: 'Search who, activity, page, IP…',
    onFilter: (q) => {
      clearTimeout(_qTimer);
      _qTimer = setTimeout(() => { if (q !== state.q) { state.q = q; load(); } }, 300);   // searched on the server
      return null;                                     // the count comes with the results (the pill), not "on this page"
    },
  });
  document.querySelectorAll('#who-filter button').forEach(b => b.addEventListener('click', () => {
    state.role = b.dataset.role;
    document.querySelectorAll('#who-filter button').forEach(x => x.classList.toggle('active', x === b));
    load();
  }));
  $('cat-filter').addEventListener('change', (e) => { state.category = e.target.value; load(); });
  initRangeControl((st) => { state.range = st; load(); });
}

onReady(init);
