/* ────────────────────────────────────────────────────────────────────────────
 * SIGN IN — users (made on the Users page), admin accounts (made by the Trinetra team on the
 * Admin Accounts page) and the Trinetra team (login.html?team=1).
 * ──────────────────────────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
try { document.documentElement.dataset.theme = localStorage.getItem('wa-theme') || 'light'; } catch { /* */ }
const params = new URLSearchParams(location.search);
let ME = {};

/** only follow ?next= to one of our own pages */
function nextPage() {
  const n = params.get('next') || '';
  return /^[a-z-]+\.html(\?[A-Za-z0-9=&%._-]*)?$/.test(n) && !n.startsWith('login.html') ? n : (ME.first_page || '/index.html').replace(/^\//, '');
}

async function loadMe() {
  try { ME = await fetch('/api/me', { credentials: 'same-origin' }).then(r => r.json()); } catch (_) { ME = {}; }
}

async function submit(ev) {
  ev.preventDefault();
  const btn = $('login-btn'), err = $('login-error');
  btn.disabled = true; btn.textContent = 'Signing in…'; err.hidden = true;
  try {
    const r = await fetch('/api/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin',
      body: JSON.stringify({ username: $('login-user').value.trim(), password: $('login-pass').value }),
    });
    if (r.ok) {
      await loadMe();
      if (!ME.first_page) { err.textContent = 'Signed in, but this account has no pages yet — ask your administrator.'; err.hidden = false; return; }
      location.replace(nextPage());
      return;
    }
    const data = await r.json().catch(() => ({}));
    err.textContent = r.status === 429 ? (data.detail || 'Too many failed sign-ins — try again in 15 minutes') : 'Wrong user ID or password.';
  } catch (_) {
    err.textContent = 'Cannot reach the server.';
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
  err.hidden = false;
  $('login-pass').value = '';
  $('login-pass').focus();
}

async function init() {
  await loadMe();
  const team = params.get('team') === '1';
  $('team-link').hidden = team || !ME.support_enabled;
  if (params.get('next')) $('team-link').href = `login.html?team=1&next=${encodeURIComponent(params.get('next'))}`;
  if (team) {
    $('login-title').textContent = 'Trinetra team login';
    $('login-sub').textContent = 'For Trinetra Systems staff. Shows DeepStream, Settings, Admin Accounts and Activity Logs.';
  }
  if (ME.expired) $('login-sub').textContent = 'Your session has ended — please sign in again.';
  if (ME.signed_in) {
    $('signed-in').hidden = false;
    $('signed-in-name').textContent = ME.name && ME.name !== ME.username ? `${ME.name} (${ME.username})` : ME.username;
    $('continue-btn').href = nextPage();
    $('login-form').hidden = true;
  }
  $('switch-btn').addEventListener('click', async () => {
    await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
    location.reload();
  });
  $('login-form').addEventListener('submit', submit);
}
init();
