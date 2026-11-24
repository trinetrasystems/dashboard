/* ────────────────────────────────────────────────────────────────────────────
 * USERS — the admin creates sign-ins for other people and picks their permissions.
 * ──────────────────────────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);

const state = { users: [], q: '', editing: null };

// Short descriptions shown under each permission checkbox (from /api/me: all_permissions[].help)
const PERM_HELP = new Proxy({}, { get: (_, k) => ME.all_permissions.find(p => p.key === k)?.help || '' });

const permLabel = (k) => ME.all_permissions.find(p => p.key === k)?.label || k;

function fmtWhen(s) {
  if (!s) return '<span class="muted">Never</span>';
  const d = new Date(s.replace(' ', 'T'));
  return `${escapeHtml(fmtDay(d))} · ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function visibleUsers() {
  return state.users.filter(u => matchesQuery(state.q, [u.username, u.name, ...u.permissions.map(permLabel), u.active ? 'active' : 'disabled']));
}

const userPager = pager('user-pager', () => render());

function render() {
  const all = visibleUsers(), rows = userPager.slice(all);
  $('user-count').textContent = all.length === state.users.length ? all.length : `${all.length} / ${state.users.length}`;
  $('user-body').innerHTML = rows.length ? rows.map(u => `<tr class="${u.active ? '' : 'disabled-row'}">
      <td><div class="emp-cell"><div class="avatar sm">${escapeHtml(initials(u.name || u.username))}</div>
        <div><div class="emp-cell-name">${escapeHtml(u.name || u.username)}</div><div class="emp-cell-sub">ID: ${escapeHtml(u.username)}</div></div></div></td>
      <td><div class="perm-chips">${u.permissions.length ? u.permissions.map(p => `<span class="tag dept">${escapeHtml(permLabel(p))}</span>`).join('') : '<span class="muted">Nothing yet</span>'}</div></td>
      <td>${u.active ? '<span class="tag on_desk">Active</span>' : '<span class="tag">Disabled</span>'}</td>
      <td class="nowrap">${fmtWhen(u.last_login)}</td>
      <td class="row-actions">
        <button class="btn sm" data-edit="${u.id}">${icon('pencil')}Edit</button>
        <button class="btn sm" data-toggle="${u.id}" title="${u.active ? 'Sign this user out and block sign-in' : 'Allow this user to sign in again'}">${u.active ? 'Disable' : 'Enable'}</button>
        <button class="icon-btn sm" data-del="${u.id}" aria-label="Delete user" title="Delete user">${icon('trash')}</button>
      </td></tr>`).join('')
    : `<tr><td colspan="5" class="empty">${state.users.length ? 'No users match.' : 'No users yet — create one with “New User”.'}</td></tr>`;
}

async function load() {
  try { state.users = await apiGet('/api/users'); render(); }
  catch (err) { toast(`Could not load users: ${err.message}`); }
}

/* Enable / Disable from the row: disabling signs the user out at once and blocks sign-in until enabled again */
async function toggleUser(id) {
  const u = state.users.find(x => x.id === Number(id));
  if (!u) return;
  if (u.active && !confirm(`Disable ${u.name || u.username}? They are signed out at once and cannot sign in until enabled again.`)) return;
  try {
    await apiPut(`/api/users/${u.id}`, { name: u.name, permissions: u.permissions, active: !u.active });
    toast(`${u.username} ${u.active ? 'disabled' : 'enabled'}`);
    load();
  } catch (err) { toast(err.message); }
}

/* ─── Modal ─── */
function permChecks(selected) {
  $('perm-grid').innerHTML = ME.all_permissions.map(p => `
    <label class="perm"><input type="checkbox" name="perm" value="${p.key}" ${selected.includes(p.key) ? 'checked' : ''}>
      <span><b>${escapeHtml(p.label)}</b><small>${escapeHtml(PERM_HELP[p.key] || '')}</small></span></label>`).join('');
}

function openModal(u) {
  state.editing = u || null;
  const f = $('user-form').elements;
  $('user-form').reset();
  $('user-modal-title').textContent = u ? `Edit ${u.name || u.username}` : 'New User';
  $('user-save').textContent = u ? 'Save changes' : 'Create user';
  $('pw-label').textContent = u ? 'New password (leave blank to keep)' : 'Password';
  f.username.value = u?.username || '';
  f.username.disabled = !!u;
  f.name.value = u?.name || '';
  f.active.checked = u ? u.active : true;
  f.password.required = !u;
  permChecks(u ? u.permissions : ['live', 'logs']);
  $('user-result').textContent = '';
  $('user-modal').hidden = false;
  (u ? f.name : f.username).focus();
}
function closeModal() { $('user-modal').hidden = true; state.editing = null; }

async function save(ev) {
  ev.preventDefault();
  const f = $('user-form').elements;
  const res = $('user-result');
  res.className = 'form-result bad';
  if (f.password.value !== f.password2.value) { res.textContent = 'The two passwords don’t match.'; return; }
  const body = {
    name: f.name.value.trim(),
    permissions: [...document.querySelectorAll('#perm-grid input:checked')].map(x => x.value),
    active: f.active.checked,
    ...(f.password.value ? { password: f.password.value } : {}),
  };
  try {
    if (state.editing) {
      await apiPut(`/api/users/${state.editing.id}`, body);
      toast(`Saved ${body.name || state.editing.username}`);
    } else {
      const u = await apiPost('/api/users', { ...body, username: f.username.value.trim() });
      toast(`Created ${u.username} — they can sign in at ${location.origin}/login.html`);
    }
    closeModal();
    load();
  } catch (err) {
    res.textContent = err.message;
  }
}

async function remove(id) {
  const u = state.users.find(x => x.id === Number(id));
  if (!u || !confirm(`Delete ${u.name || u.username}? They will no longer be able to sign in.`)) return;
  try { await apiDelete(`/api/users/${id}`); toast(`${u.username} deleted`); load(); } catch (err) { toast(err.message); }
}

function init() {
  initSearch({
    placeholder: 'Search users…',
    onFilter: (q) => { state.q = q; render(); return visibleUsers().length; },
  });
  $('new-user-btn').addEventListener('click', () => openModal(null));
  $('user-body').addEventListener('click', (e) => {
    const ed = e.target.closest('[data-edit]');
    if (ed) openModal(state.users.find(u => u.id === Number(ed.dataset.edit)));
    const del = e.target.closest('[data-del]');
    if (del) remove(del.dataset.del);
    const tg = e.target.closest('[data-toggle]');
    if (tg) toggleUser(tg.dataset.toggle);
  });
  $('perm-all').addEventListener('click', () => document.querySelectorAll('#perm-grid input').forEach(x => { x.checked = true; }));
  $('perm-none').addEventListener('click', () => document.querySelectorAll('#perm-grid input').forEach(x => { x.checked = false; }));
  // Editing desks / acknowledging alerts implies seeing those pages
  $('perm-grid').addEventListener('change', (e) => {
    const v = e.target.value, on = e.target.checked;
    const pair = { manage_alerts: 'live', export: 'logs' };
    if (on && pair[v]) document.querySelector(`#perm-grid input[value=${pair[v]}]`).checked = true;
    const rev = Object.entries(pair).find(([, page]) => page === v);
    if (!on && rev) document.querySelector(`#perm-grid input[value=${rev[0]}]`).checked = false;
  });
  $('user-form').addEventListener('submit', save);
  $('user-close').addEventListener('click', closeModal);
  $('user-cancel').addEventListener('click', closeModal);
  $('user-modal').addEventListener('click', (e) => { if (e.target.id === 'user-modal') closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('user-modal').hidden) closeModal(); });
  load();
}

onReady(init);
