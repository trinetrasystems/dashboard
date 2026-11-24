/* ────────────────────────────────────────────────────────────────────────────
 * ADMIN ACCOUNTS — the Trinetra team creates admin logins for the client.
 * The password (typed or generated) is shown in the credentials panel after create / reset, and again
 * whenever the Trinetra team clicks the admin's row ("Credentials": /api/admins/<id>/credentials);
 * the list (GET /api/admins) never carries it.
 * ──────────────────────────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);

const state = { admins: [], q: '' };

function fmtWhen(s) {
  if (!s) return '<span class="muted">Never</span>';
  const d = new Date(s.replace(' ', 'T'));
  return `${escapeHtml(fmtDay(d))} · ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function visibleAdmins() {
  return state.admins.filter(a => matchesQuery(state.q, [a.username, a.name, a.created_by, a.active ? 'active' : 'disabled']));
}

const adminPager = pager('admin-pager', () => render());

function render() {
  const all = visibleAdmins(), rows = adminPager.slice(all);
  $('admin-count').textContent = all.length === state.admins.length ? all.length : `${all.length} / ${state.admins.length}`;
  $('admin-body').innerHTML = rows.length ? rows.map(a => `<tr class="clickable ${a.active ? '' : 'disabled-row'}" data-admin="${escapeHtml(a.username)}" data-cred="${a.id}" title="Click to see the sign-in details">
      <td><div class="emp-cell"><div class="avatar sm">${escapeHtml(initials(a.name || a.username))}</div>
        <div><div class="emp-cell-name">${escapeHtml(a.name || a.username)}</div><div class="emp-cell-sub">ID: ${escapeHtml(a.username)}</div></div></div></td>
      <td>${a.active ? '<span class="tag on_desk">Active</span>' : '<span class="tag">Disabled</span>'}</td>
      <td class="nowrap">${fmtWhen(a.created_at)}${a.created_by ? `<div class="emp-cell-sub">by ${escapeHtml(a.created_by)}</div>` : ''}</td>
      <td class="nowrap">${fmtWhen(a.last_login)}</td>
      <td class="row-actions">
        <button class="btn sm" data-cred="${a.id}" title="User ID and password of this admin">${icon('key')}Credentials</button>
        <button class="btn sm" data-reset="${a.id}" title="Type a new password for this admin (signs them out)">Change password</button>
        <button class="btn sm" data-toggle="${a.id}">${a.active ? 'Disable' : 'Enable'}</button>
        ${a.active ? '' : `<button class="icon-btn sm" data-del="${a.id}" aria-label="Delete admin" title="Delete admin">${icon('trash')}</button>`}
      </td></tr>`).join('')
    : `<tr><td colspan="5" class="empty">${state.admins.length ? 'No admins match.' : 'No admin accounts yet — create one with “New Admin”.'}</td></tr>`;
}

async function load() {
  try { state.admins = await apiGet('/api/admins'); render(); }
  catch (err) { toast(`Could not load admin accounts: ${err.message}`); }
}

/* ─── New admin ─── */
function openForm() {
  $('admin-form').reset();
  $('admin-result').textContent = '';
  $('admin-modal').hidden = false;
  $('admin-form').elements.username.focus();
}
function closeForm() { $('admin-modal').hidden = true; $('admin-form').reset(); }

async function create(ev) {
  ev.preventDefault();
  const f = $('admin-form').elements;
  const res = $('admin-result');
  res.className = 'form-result bad';
  $('admin-save').disabled = true;
  try {
    const a = await apiPost('/api/admins', {
      username: f.username.value.trim(), name: f.name.value.trim(), ...(f.password.value ? { password: f.password.value } : {}),
    });
    closeForm();
    showCredentials(a, `Admin ${a.username} created`);
    load();
  } catch (err) {
    res.textContent = err.message;
  } finally {
    $('admin-save').disabled = false;
  }
}

/* ─── Credentials panel (shown once) ─── */
const credText = () => `Trinetra Perimeter Dashboard — admin sign-in\nSign-in page: ${$('cred-url').textContent}\n`
  + `User ID: ${$('cred-user').textContent}\nPassword: ${$('cred-pass').textContent}`;

function showCredentials(a, title) {
  $('cred-title').textContent = title;
  $('cred-url').textContent = a.login_url || `${location.origin}/login.html`;   // the client's address (PUBLIC_URL), not ours
  $('cred-user').textContent = a.username;
  $('cred-pass').textContent = a.password || 'Shown after this admin next signs in (or use “Change password” now)';
  $('cred-copy').disabled = !a.password;
  $('cred-result').textContent = '';
  $('cred-modal').hidden = false;
  $('cred-copy').focus();
}
function closeCredentials() {
  $('cred-modal').hidden = true;
  $('cred-pass').textContent = '';                    // not kept in the page once the panel is closed
}

async function copyCredentials() {
  const ok = await copyToClipboard(credText());
  $('cred-result').className = `form-result ${ok ? 'good' : 'bad'}`;
  $('cred-result').textContent = ok ? 'Copied.' : 'Could not copy — select the text and copy it by hand.';
}

/* ─── Row actions ─── */
/* the Trinetra team types the admin's new password (the old one stops working, the admin is signed out) */
let _pwFor = null;
function resetPassword(id) {
  _pwFor = state.admins.find(x => x.id === Number(id));
  if (!_pwFor) return;
  $('pw-form').reset();
  $('pw-title').textContent = `New password for ${_pwFor.name || _pwFor.username}`;
  $('pw-result').textContent = '';
  $('pw-modal').hidden = false;
  $('pw-form').elements.password.focus();
}
function closePw() { $('pw-modal').hidden = true; $('pw-form').reset(); _pwFor = null; }

async function savePw(ev) {
  ev.preventDefault();
  const f = $('pw-form').elements, res = $('pw-result'), a = _pwFor;
  res.className = 'form-result bad';
  if (f.password.value !== f.password2.value) { res.textContent = 'The two passwords don’t match.'; return; }
  $('pw-save').disabled = true;
  try {
    if (f.password.value && f.password.value.length < 12) { res.textContent = 'At least 12 characters (or leave both blank to generate one).'; $('pw-save').disabled = false; return; }
    const out = await apiPost(`/api/admins/${a.id}/reset-password`, f.password.value ? { password: f.password.value } : {});
    closePw();
    showCredentials(out, `New password for ${a.username}`);
    load();
  } catch (err) { res.textContent = err.message; }
  finally { $('pw-save').disabled = false; }
}

async function showSaved(id) {
  try {
    const a = await apiGet(`/api/admins/${id}/credentials`);
    showCredentials(a, `Credentials of ${a.name || a.username}`);
  } catch (err) { toast(err.message); }
}

async function toggle(id) {
  const a = state.admins.find(x => x.id === Number(id));
  if (!a) return;
  if (a.active && !confirm(`Disable ${a.name || a.username}? They are signed out at once and cannot sign in with this account until it is enabled again.`)) return;
  try { await apiPut(`/api/admins/${id}`, { active: !a.active }); toast(`${a.username} ${a.active ? 'disabled' : 'enabled'}`); load(); }
  catch (err) { toast(err.message); }
}

async function remove(id) {
  const a = state.admins.find(x => x.id === Number(id));
  if (!a || !confirm(`Delete the admin account ${a.username}? This cannot be undone (the users they created stay).`)) return;
  try { await apiDelete(`/api/admins/${id}`); toast(`${a.username} deleted`); load(); }
  catch (err) { toast(err.message); }
}

function init() {
  initSearch({
    placeholder: 'Search admin accounts…',
    onFilter: (q) => { state.q = q; render(); return visibleAdmins().length; },
  });
  $('new-admin-btn').addEventListener('click', openForm);
  $('admin-body').addEventListener('click', (e) => {
    const b = e.target.closest('[data-reset],[data-toggle],[data-del]');
    if (!b) { const r = e.target.closest('[data-cred]'); if (r) showSaved(r.dataset.cred); return; }   // the row or its Credentials button
    if (b.dataset.reset) resetPassword(b.dataset.reset);
    else if (b.dataset.toggle) toggle(b.dataset.toggle);
    else remove(b.dataset.del);
  });
  $('admin-form').addEventListener('submit', create);
  $('pw-form').addEventListener('submit', savePw);
  $('pw-close').addEventListener('click', closePw);
  $('pw-cancel').addEventListener('click', closePw);
  $('admin-close').addEventListener('click', closeForm);
  $('admin-cancel').addEventListener('click', closeForm);
  $('admin-modal').addEventListener('click', (e) => { if (e.target.id === 'admin-modal') closeForm(); });
  $('cred-copy').addEventListener('click', copyCredentials);
  $('cred-done').addEventListener('click', closeCredentials);
  // The credentials panel closes only with "Done" (a stray click outside must not lose the password)
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('admin-modal').hidden) closeForm();
    if (!$('pw-modal').hidden) closePw();
  });
  load();
}

onReady(init);
