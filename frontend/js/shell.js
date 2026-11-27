/* ────────────────────────────────────────────────────────────────────────────
 * SHELL — the Workplace Analytics page frame for the perimeter dashboard: icons, sidebar, theme, sign-in check,
 * own password, toast, pager. Loaded first on every page (then api.js, common.js, the page's script).
 * Icons, pager and the own-password dialog are the Workplace Analytics code (frontend/js/common.js).
 * ──────────────────────────────────────────────────────────────────────────── */

/* ─── Icons (24×24 stroke icons, lucide-style) ─── */
const ICONS = {
  'shield':     '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  'eye':        '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  'list':       '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  'route':      '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
  'check-circle': '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/>',
  'dashboard':  '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  'video':      '<path d="m16 13 5.2 3.5a.5.5 0 0 0 .8-.4V7.9a.5.5 0 0 0-.8-.4L16 10.5"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
  'monitor':    '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8M12 17v4"/>',
  'users':      '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  'user':       '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  'line-chart': '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/>',
  'bar-chart':  '<path d="M3 3v18h18"/><path d="M18 17V9M13 17V5M8 17v-3"/>',
  'bell':       '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  'speaker':    '<path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a10 10 0 0 1 0 14"/>',
  'camera':     '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>',
  'settings':   '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  'calendar':   '<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  'download':   '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5M12 15V3"/>',
  'pencil':     '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
  'building':   '<rect width="16" height="20" x="4" y="2" rx="2"/><path d="M9 22v-4h6v4M8 6h.01M12 6h.01M16 6h.01M8 10h.01M12 10h.01M16 10h.01M8 14h.01M12 14h.01M16 14h.01"/>',
  'map-pin':    '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
  'desk':       '<path d="M2 8h20M4 8v12M20 8v12M14 8v6h6M14 11h6"/>',
  'smartphone': '<rect width="14" height="20" x="5" y="2" rx="2"/><path d="M12 18h.01"/>',
  'walk':       '<circle cx="13" cy="4" r="2"/><path d="m9 21 2.5-5.5L15 18v3M7 11.5 10 8l3.5 1 2.5 3h3M11.5 15.5 10.5 8"/>',
  'info':       '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  'trend-up':   '<path d="m22 7-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
  'trend-down': '<path d="m22 17-8.5-8.5-5 5L2 7"/><path d="M16 17h6v-6"/>',
  'lightbulb':  '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6M10 22h4"/>',
  'clock':      '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  'chevron-right': '<path d="m9 18 6-6-6-6"/>',
  'chevron-down':  '<path d="m6 9 6 6 6-6"/>',
  'arrow-up':   '<path d="M12 19V5M5 12l7-7 7 7"/>',
  'arrow-down': '<path d="M12 5v14M19 12l-7 7-7-7"/>',
  'sun':        '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  'moon':       '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  'x':          '<path d="M18 6 6 18M6 6l12 12"/>',
  'menu':       '<path d="M4 6h16M4 12h16M4 18h16"/>',
  'flask':      '<path d="M9 3h6M10 3v6.5L4.6 18.3A1.8 1.8 0 0 0 6.1 21h11.8a1.8 1.8 0 0 0 1.5-2.7L14 9.5V3"/><path d="M7 15h10"/>',
  'upload':     '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5M12 3v12"/>',
  'trash':      '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  'image':      '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
  'plus':       '<path d="M12 5v14M5 12h14"/>',
  'refresh':    '<path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5"/>',
  'log-out':    '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  'file-text':  '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4M10 9H8M16 13H8M16 17H8"/>',
  'table':      '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/>',
  'folder':     '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9l-.8-1.2A2 2 0 0 0 7.9 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  'lock':       '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  'key':        '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/>',
  'copy':       '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  'mail':       '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
  'check':      '<path d="M20 6 9 17l-5-5"/>',
  'package':    '<path d="M16.5 9.4 7.55 4.24M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.3 7 12 12l8.7-5M12 22V12"/>',
  'wifi-off':   '<path d="M12 20h.01M8.5 16.4a5 5 0 0 1 7 0M2 8.8a15 15 0 0 1 4.2-2.7M22 8.8a15 15 0 0 0-11.3-3.8M5 12.9a10 10 0 0 1 5.2-2.7M19 12.9a10 10 0 0 0-2.3-1.6M2 2l20 20"/>',
  'search':     '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  'door':       '<path d="M13 4h3a2 2 0 0 1 2 2v14M2 20h3M13 20h9M10 12v.01"/><path d="M13 4.6v16.2a.6.6 0 0 1-.8.6L5 19V5.6a1 1 0 0 1 .8-1l6.6-1.6a.6.6 0 0 1 .6.6Z"/>',
  'radio':      '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5M19.1 4.9C23 8.8 23 15.1 19.1 19"/><circle cx="12" cy="12" r="2"/>',
  'alert':      '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z"/><path d="M12 9v4M12 17h.01"/>',
};

function icon(name, cls = '') {
  return `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
}

/** Fill every [data-icon] element with its SVG (keeps existing text after it). */
function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(el => {
    el.insertAdjacentHTML('afterbegin', icon(el.dataset.icon));
    el.removeAttribute('data-icon');
  });
}

/* ─── Formatters ─── */

function initials(name) {
  return String(name || '?').trim().split(/\s+/).slice(0, 2).map(p => p[0]).join('').toUpperCase();
}
/** Live updates come every few seconds when many desks are watched: reload a view at most once per `gap` ms
 *  (the first one 0.8 s after a change), and not at all while the tab is in the background — once when it is back. */

const PHONE_MQ = matchMedia('(max-width: 640px)');
function pager(id, onChange, size = 10) {
  const st = { page: 1, used: 1, total: 0, get size() { const n = typeof size === 'function' ? size() : size; return PHONE_MQ.matches ? Math.min(5, n) : n; } };
  PHONE_MQ.addEventListener('change', () => {          // rotated across the phone width: keep the first row in view
    if (!st.total) return;
    st.page = Math.floor((st.page - 1) * st.used / st.size) + 1; onChange();
  });
  const el = () => document.getElementById(id);
  const go = (p) => { st.page = p; onChange(); };
  document.addEventListener('click', (e) => {
    const b = e.target.closest(`#${id} [data-pg]`);
    if (b && !b.disabled) { go(Number(b.dataset.pg)); el()?.closest('.card')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
  });
  const put = (box, html) => { if (box.__pg !== html) { box.innerHTML = html; box.__pg = html; } };   // live pages redraw often
  function draw() {
    const box = el(); if (!box) return;
    const pages = Math.max(1, Math.ceil(st.total / st.size));
    if (st.total <= st.size) { put(box, st.total ? `<span class="pg-info">${st.total} ${st.total === 1 ? 'entry' : 'entries'}</span>` : ''); return; }
    const a = (st.page - 1) * st.size + 1, b = Math.min(st.total, st.page * st.size);
    const nums = [];
    for (let p = 1; p <= pages; p++) {
      if (p === 1 || p === pages || Math.abs(p - st.page) <= 1) nums.push(p);
      else if (nums[nums.length - 1] !== '…') nums.push('…');
    }
    const btn = (p, label, aria, on) => `<button type="button" class="pg-btn${on ? ' on' : ''}" data-pg="${p}"${p < 1 || p > pages || on ? ' disabled' : ''} aria-label="${aria}">${label}</button>`;
    put(box, `<span class="pg-info">${a}–${b} of ${st.total}</span><span class="pg-nav">` +
      btn(st.page - 1, '‹', 'Previous page') +
      nums.map(p => p === '…' ? '<span class="pg-gap">…</span>' : btn(p, p, `Page ${p}`, p === st.page)).join('') +
      btn(st.page + 1, '›', 'Next page') + '</span>');
  }
  return {
    slice(rows, total = rows.length) {                 // total: more rows than loaded (the page loads them when reached)
      st.total = total; st.used = st.size;
      const pages = Math.max(1, Math.ceil(st.total / st.size));
      if (st.page > pages) st.page = pages;
      draw();
      return rows.slice((st.page - 1) * st.size, st.page * st.size);
    },
    total(n) {                                       // server-paged lists: the server says how many rows there are
      st.total = n; st.used = st.size;
      const pages = Math.max(1, Math.ceil(st.total / st.size));
      if (st.page > pages) st.page = pages;
      draw();
    },
    reset() { st.page = 1; },
    get page() { return st.page; },
    get size() { return st.size; },
    go,
  };
}

/** Copy text; falls back to a hidden textarea where navigator.clipboard is missing (plain http on the LAN). */

async function copyToClipboard(text) {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; } } catch { /* fall back */ }
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

/** A small version of an archived camera image (tables, cards): /archive/<day>/<file> → /thumbs/<day>/<file>?w= */

/* ─── Toast ─── */
let _toastTimer;
function toast(msg) {
  let el = document.getElementById('toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; document.body.appendChild(el); }
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

/* ─── Theme (light by default, as Workplace Analytics) ─── */
function getTheme() {
  try { return localStorage.getItem('wa-theme') || 'light'; } catch { return 'light'; }
}
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('wa-theme', t); } catch { /* storage unavailable */ }
  const btn = document.getElementById('theme-btn');
  if (btn) btn.innerHTML = icon(t === 'dark' ? 'sun' : 'moon');
  window.dispatchEvent(new CustomEvent('themechange', { detail: t }));
}
applyTheme(getTheme());

/* ─── Access ───────────────────────────────────────────────────────────────
 * From /api/me: support (Trinetra team), admin (admin accounts), user (Users page, with permissions).
 * `need` of a page: a permission key, 'admin' (admins + team) or 'support' (team only).
 */
const NAV = [
  { section: null, items: [
    { icon: 'shield',     label: 'Live',            href: 'index.html',      need: 'live' },
    { icon: 'bell',       label: 'Alerts',          href: 'alerts.html',     need: 'live' },
    { icon: 'line-chart', label: 'Logs & Charts',   href: 'logs.html',       need: 'logs' },
    { icon: 'route',      label: 'Visits',          href: 'visits.html',     need: 'logs' },
    { icon: 'check-circle', label: 'Whitelist',     href: 'whitelist.html',  need: 'live' },
  ]},
  { section: 'Administration', items: [
    { icon: 'user',       label: 'Users',           href: 'users.html',      need: 'admin' },
  ]},
  { section: 'Trinetra', items: [
    { icon: 'video',      label: 'DeepStream',      href: 'deepstream.html', need: 'support' },
    { icon: 'camera',     label: 'Recordings',      href: 'recordings.html', need: 'support' },
    { icon: 'settings',   label: 'Settings',        href: 'admin.html',      need: 'support' },
    { icon: 'key',        label: 'Admin Accounts',  href: 'admins.html',     need: 'support' },
    { icon: 'file-text',  label: 'Activity Logs',   href: 'activity.html',   need: 'support' },
  ]},
];
const PAGE_NEED = Object.fromEntries(NAV.flatMap(g => g.items).map(it => [it.href, it.need]));

let ME = { role: 'anonymous', permissions: [] };

function can(need) {
  if (!need || ME.role === 'support') return true;
  if (ME.role === 'anonymous' || need === 'support') return false;
  if (ME.role === 'admin') return true;
  if (need === 'admin') return false;
  return (ME.permissions || []).includes(need);
}
const canSee = (me, need) => { const keep = ME; ME = me || ME; try { return can(need); } finally { ME = keep; } };

function currentPage() { return location.pathname.split('/').pop() || 'index.html'; }
function firstAllowedPage() { return (ME.first_page || '').replace(/^\//, '') || NAV.flatMap(g => g.items).find(it => can(it.need))?.href; }

async function signOut() {
  if (typeof leavingPage === 'function') leavingPage();      // api.js: the page's polls stop before the session ends,
  if (typeof requestsSettled === 'function') await requestsSettled();   // and those on their way finish first
  try { await fetch('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: '{}' }); } catch (_) { }
  location.replace('login.html');
}

/** who is signed in; not signed in → the sign-in page (back here afterwards) */
const accessReady = (async () => {
  try { ME = await fetch('/api/me', { credentials: 'same-origin' }).then(r => r.json()); } catch (_) { return false; }
  if (!ME.signed_in) {
    const page = currentPage();
    location.replace(page === 'index.html' ? 'login.html' : `login.html?next=${encodeURIComponent(page + location.search + location.hash)}`);
    return false;
  }
  return true;
})();
function getMe() { return accessReady.then(() => ME); }

/** Run a page's init once access is confirmed (the shell is drawn first). */
function onReady(fn) {
  document.addEventListener('DOMContentLoaded', () => accessReady.then(ok => {
    if (!ok) return;
    renderShell();
    fn();
  }));
}

/* ─── Change your own password (admin accounts and users) ─── */
function openOwnPassword() {
  let m = document.getElementById('own-pw-modal');
  if (!m) {
    document.body.insertAdjacentHTML('beforeend', `
      <div class="modal-backdrop" id="own-pw-modal" hidden>
        <form class="modal form" id="own-pw-form" autocomplete="off">
          <div class="card-head"><h2 class="card-title">Change password</h2>
            <button type="button" class="icon-btn" id="own-pw-close" aria-label="Close">${icon('x')}</button></div>
          <label>Current password <input name="current" type="password" required autocomplete="current-password" maxlength="128"></label>
          <label>New password <input name="new" type="password" required minlength="12" maxlength="128" autocomplete="new-password" placeholder="At least 12 characters"></label>
          <label>Repeat new password <input name="new2" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label>
          <div class="form-result" id="own-pw-result"></div>
          <div class="pop-actions"><span class="spacer"></span>
            <button type="button" class="btn ghost" id="own-pw-cancel">Cancel</button>
            <button type="submit" class="btn primary" id="own-pw-save">Change password</button></div>
        </form>
      </div>`);
    m = document.getElementById('own-pw-modal');
    const close = () => { m.hidden = true; document.getElementById('own-pw-form').reset(); };
    document.getElementById('own-pw-close').addEventListener('click', close);
    document.getElementById('own-pw-cancel').addEventListener('click', close);
    document.getElementById('own-pw-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const f = ev.target.elements, res = document.getElementById('own-pw-result'), btn = document.getElementById('own-pw-save');
      res.className = 'form-result bad';
      if (f.new.value !== f.new2.value) { res.textContent = 'The two new passwords don’t match.'; return; }
      btn.disabled = true;
      try {
        await apiPost('/api/me/password', { current: f.current.value, new: f.new.value });
        close();
        toast('Password changed. Other devices were signed out.');
      } catch (err) { res.textContent = err.message; }
      finally { btn.disabled = false; }
    });
  }
  document.getElementById('own-pw-result').textContent = '';
  m.hidden = false;
  document.getElementById('own-pw-form').elements.current.focus();
}

/* ─── Sidebar (as Workplace Analytics) ─── */
function renderSidebar() {
  const el = document.getElementById('sidebar');
  if (!el) return;
  const page = currentPage();
  const groups = NAV.map(g => ({ ...g, items: g.items.filter(it => can(it.need)) })).filter(g => g.items.length).map(g => `
    ${g.section ? `<div class="nav-section">${g.section}${g.section === 'Trinetra' ? ' <span class="nav-badge">team only</span>' : ''}</div>` : ''}
    <nav class="nav">
      ${g.items.map(it => `
        <a class="nav-item ${it.href === page ? 'active' : ''}" href="${it.href}">
          ${icon(it.icon)}<span>${it.label}</span>
        </a>`).join('')}
    </nav>`).join('');
  const who = {
    admin:   { name: ME.name || ME.username, role: `Administrator · ${ME.username}`, av: initials(ME.name || ME.username) },
    support: { name: 'Trinetra Support', role: ME.username ? `Trinetra team · ${ME.username}` : 'Trinetra team', av: 'TS' },
    user:    { name: ME.name || ME.username, role: `User · ${ME.username}`, av: initials(ME.name || ME.username) },
  }[ME.role] || { name: 'Signed out', role: '', av: '?' };
  const action = (ME.own_password ? `<button class="side-action" id="change-pw" title="Change your password">${icon('key')}<span>Change password</span></button>` : '')
    + `<button class="side-action" id="sign-out" title="Sign out">${icon('log-out')}<span>Sign out</span></button>`;
  el.innerHTML = `
    <a class="brand" href="${firstAllowedPage() || 'index.html'}" aria-label="Trinetra Systems — Perimeter Security home">
      <img class="brand-img logo-light" src="img/trinetra-logo-light.png" alt="Trinetra Systems">
      <img class="brand-img logo-dark" src="img/trinetra-logo-dark.png" alt="Trinetra Systems">
      <div class="brand-name">Perimeter Security</div>
    </a>
    ${groups}
    <div class="sidebar-user">
      <div class="avatar sm">${escapeHtml(who.av)}</div>
      <div class="who"><div class="who-name">${escapeHtml(who.name)}</div><div class="who-role">${escapeHtml(who.role)}</div></div>
    </div>
    ${action}`;
  document.getElementById('sign-out')?.addEventListener('click', signOut);
  document.getElementById('change-pw')?.addEventListener('click', openOwnPassword);
  // Menu button: hides / shows the sidebar on a wide screen (remembered), opens it over the page on a narrow one
  const open = (v) => document.body.classList.toggle('nav-open', v);
  const btn = document.getElementById('menu-btn');
  const wide = () => matchMedia('(min-width: 1101px)').matches;
  const label = () => btn?.setAttribute('aria-label', wide() ? (document.body.classList.contains('nav-collapsed') ? 'Show menu' : 'Hide menu') : 'Open menu');
  label();
  btn?.addEventListener('click', () => {
    if (!wide()) return open(true);
    const c = document.body.classList.toggle('nav-collapsed');
    try { localStorage.setItem('nav-collapsed', c ? '1' : ''); } catch { /* private mode */ }
    label();
    window.dispatchEvent(new Event('resize'));
  });
  btn && (btn.title = 'Hide / show the menu');
  document.getElementById('sidebar-backdrop')?.addEventListener('click', () => open(false));
}

/** sidebar + icons + theme button; acknowledging is shown only to those who may */
function renderShell() {
  try { if (localStorage.getItem('nav-collapsed') === '1') document.body.classList.add('nav-collapsed'); } catch { /* */ }
  renderSidebar();
  hydrateIcons();
  document.body.dataset.role = ME.role;
  if (can('manage_alerts')) document.body.dataset.canAck = '1';
  const tb = document.getElementById('theme-btn');
  if (tb) { tb.innerHTML = icon(getTheme() === 'dark' ? 'sun' : 'moon');
            tb.addEventListener('click', () => applyTheme(getTheme() === 'dark' ? 'light' : 'dark')); }
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function pad(n) { return String(n).padStart(2, '0'); }
function fmtWhen(s) {
  if (!s) return '<span class="muted">Never</span>';
  const d = new Date(String(s).replace(' ', 'T'));
  return `${d.getDate()} ${d.toLocaleString('en', { month: 'short' })} ${d.getFullYear()} · ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* ─── WA date helpers, search box and range control (frontend/js/common.js of Workplace Analytics) ─── */
function fmtDay(d) {
  d = (d instanceof Date) ? d : new Date(d);
  return `${pad(d.getDate())} ${d.toLocaleString('en', { month: 'short' })} ${d.getFullYear()}`;
}
/** "16 Oct – 22 Oct 2026" — the year is written once when both ends share it */
function fmtRange(a, b) {
  if (a.getFullYear() !== b.getFullYear()) return `${fmtDay(a)} – ${fmtDay(b)}`;
  const short = (d) => `${pad(d.getDate())} ${d.toLocaleString('en', { month: 'short' })}`;
  return `${short(a)} – ${short(b)} ${b.getFullYear()}`;
}
/** Local YYYY-MM-DD (toISOString would shift to UTC) */
function isoDay(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function parseDay(s) {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}
function addDays(d, n) {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

const normKey = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');

/** True when every word of q appears in one of the fields (desk numbers match without hyphens). */
function matchesQuery(q, fields) {
  const words = String(q || '').toLowerCase().trim().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = fields.filter(Boolean).map(String).join(' \u0001 ').toLowerCase();
  const hayKey = normKey(hay);
  return words.every(w => hay.includes(w) || (normKey(w) && hayKey.includes(normKey(w))));
}


/** the WA search box: own row under the page head; filters the page (onFilter(q) → count shown nowhere else) */
function initSearch({ placeholder = 'Search…', onFilter = null } = {}) {
  const host = document.querySelector('.page-head');
  if (!host) return null;
  host.querySelector('.head-controls').insertAdjacentHTML('afterend', `
    <div class="search" role="search">
      ${icon('search')}
      <input id="search-input" type="search" placeholder="${escapeHtml(placeholder)}" autocomplete="off" aria-label="Search">
      <kbd>/</kbd>
    </div>`);
  const input = document.getElementById('search-input');
  let timer;
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => onFilter && onFilter(input.value.trim()), 120); });
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !e.target.closest('input, textarea, select')) { e.preventDefault(); input.focus(); }
    if (e.key === 'Escape' && document.activeElement === input) { input.value = ''; onFilter && onFilter(''); input.blur(); }
  });
  return input;
}

function initRangeControl(onChange) {
  const $id = (id) => document.getElementById(id);
  const st = { range: 'today', from: isoDay(new Date()), to: isoDay(new Date()) };

  const label = () => st.from === st.to
    ? (st.to === isoDay(new Date()) ? `Today · ${fmtDay(new Date())}` : fmtDay(parseDay(st.to)))
    : fmtRange(parseDay(st.from), parseDay(st.to));

  function set(range, from, to) {
    const today = new Date();
    st.range = range;
    if (range === 'today') { st.from = st.to = isoDay(today); }
    else if (range === 'week') { st.from = isoDay(addDays(today, -6)); st.to = isoDay(today); }
    else if (range === 'month') { st.from = isoDay(addDays(today, -29)); st.to = isoDay(today); }
    else { st.from = from; st.to = to; }
    document.querySelectorAll('#range-seg button').forEach(b =>
      b.classList.toggle('active', b.dataset.range === (range === 'day' ? '' : range)));
    const p = new URLSearchParams(location.search);
    ['from', 'to'].forEach(k => p.delete(k));
    p.set('range', st.range);
    if (range === 'custom' || range === 'day') { p.set('from', st.from); p.set('to', st.to); }
    history.replaceState(null, '', `?${p}${location.hash}`);   // keep the page tab (#rules …)
    $id('date-label').textContent = label();
    $id('date-input').value = st.to;
    onChange(st);
  }

  document.querySelectorAll('#range-seg button').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.range === 'custom') {
      $id('custom-from').value = st.from;
      $id('custom-to').value = st.to;
      $id('custom-pop').hidden = false;
      return;
    }
    $id('custom-pop').hidden = true;
    set(b.dataset.range);
  }));
  $id('custom-cancel').addEventListener('click', () => { $id('custom-pop').hidden = true; });
  $id('custom-apply').addEventListener('click', () => {
    const f = $id('custom-from').value, t = $id('custom-to').value;
    if (!f || !t) { toast('Pick both dates'); return; }
    $id('custom-pop').hidden = true;
    set('custom', f <= t ? f : t, f <= t ? t : f);
  });
  const di = $id('date-input');
  di.max = isoDay(new Date());
  $id('date-pill').addEventListener('click', (e) => {
    if (e.target === di) return;
    try { di.showPicker(); } catch { di.focus(); }
  });
  di.addEventListener('change', () => {
    if (!di.value) return;
    if (di.value === isoDay(new Date())) set('today'); else set('day', di.value, di.value);
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $id('custom-pop').hidden = true; });

  const p = new URLSearchParams(location.search);
  const r = p.get('range');
  const okDay = (x) => /^\d{4}-\d{2}-\d{2}$/.test(x || '');
  if ((r === 'custom' || r === 'day' || !r) && okDay(p.get('from')) && okDay(p.get('to'))) {   // e.g. a report email's link
    const [a, b] = [p.get('from'), p.get('to')].sort();
    if (a === b && a === isoDay(new Date())) set('today');                         // an alert email opened the same day: live
    else set(a === b ? 'day' : 'custom', a, b);
  }
  else set(['today', 'week', 'month'].includes(r) ? r : 'today');
  return st;
}


/* ════════════════════════════════════════════════════════════════════════════
 * ALERT STRIP — every page (offline sites: the dashboard is where alerts are seen). While an alert is open (not
 * acknowledged) a strip flashes on top of the page (red = critical, amber = warning), the browser tab title blinks
 * and a new alert beeps (Web Audio — no sound file, no internet; browsers allow sound after the first click on the
 * page). The Live page shows its own big banner instead of the strip, but beeps the same.
 * ════════════════════════════════════════════════════════════════════════════ */
const ALERT_STRIP = { last: null, ctx: null, muted: localStorage.getItem('alert-muted') === '1', title: document.title, blink: null, open: 0 };
document.head.insertAdjacentHTML('beforeend', `<style>
  .alert-strip { position: sticky; top: 0; z-index: 40; display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 10px 14px; border-radius: 12px; border: 2px solid var(--amber); background: var(--amber-bg); color: var(--text);
    box-shadow: var(--shadow-lg); }
  .alert-strip.crit { border-color: var(--red); background: var(--red-bg); animation: strip-flash 1.1s ease-in-out infinite; }
  .alert-strip[hidden] { display: none; }
  .alert-strip .as-icon { width: 34px; height: 34px; border-radius: 8px; display: grid; place-items: center; color: #fff; background: var(--amber); flex: none; }
  .alert-strip.crit .as-icon { background: var(--red); }
  .alert-strip .as-main { flex: 1; min-width: 200px; }
  .alert-strip .as-title { font-weight: 700; }
  .alert-strip .as-sub { font-size: 14px; color: var(--text-muted); }
  .alert-strip .btn { height: 38px; font-size: 15px; }
  @keyframes strip-flash { 0%, 100% { box-shadow: 0 0 0 0 rgba(239,68,68,.55); } 50% { box-shadow: 0 0 0 8px rgba(239,68,68,0); background: var(--surface); } }
  @media (prefers-reduced-motion: reduce) { .alert-strip.crit { animation: none; } }
</style>`);

function beep(critical) {
  if (ALERT_STRIP.muted) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    ALERT_STRIP.ctx = ALERT_STRIP.ctx || new AC();
    const c = ALERT_STRIP.ctx;
    if (c.state === 'suspended') c.resume();
    const tones = critical ? [0, 0.28, 0.56, 1.1, 1.38, 1.66] : [0, 0.35];
    tones.forEach((t, i) => {
      const o = c.createOscillator(), g = c.createGain();
      o.type = 'square'; o.frequency.value = critical ? (i % 2 ? 1040 : 880) : 660;
      g.gain.setValueAtTime(0.0001, c.currentTime + t);
      g.gain.exponentialRampToValueAtTime(0.25, c.currentTime + t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + t + 0.22);
      o.connect(g); g.connect(c.destination); o.start(c.currentTime + t); o.stop(c.currentTime + t + 0.25);
    });
  } catch (_) { /* no audio here */ }
}
['click', 'keydown'].forEach(ev => document.addEventListener(ev, () => {         // browsers allow sound after a gesture
  try { if (!ALERT_STRIP.ctx) ALERT_STRIP.ctx = new (window.AudioContext || window.webkitAudioContext)(); ALERT_STRIP.ctx.resume(); } catch (_) { }
}, { once: true }));

async function refreshAlertStrip() {
  if (!can('live') || (typeof LEAVING !== 'undefined' && LEAVING)) return;     // signing out: no more polls
  let d;
  try { d = await (typeof trackedFetch === 'function' ? trackedFetch : fetch)('/api/alerts/live', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : null); } catch (_) { return; }
  if (!d) return;
  const newest = d.latest[0];
  const maxId = Math.max(0, ...d.latest.map(a => a.id));
  if (ALERT_STRIP.last !== null && maxId > ALERT_STRIP.last) {
    const fresh = d.latest.filter(a => a.id > ALERT_STRIP.last);
    if (fresh.some(a => a.sound)) beep(fresh.some(a => a.severity === 'critical'));
    window.dispatchEvent(new CustomEvent('perimeter-alert', { detail: fresh }));
  }
  ALERT_STRIP.last = Math.max(ALERT_STRIP.last || 0, maxId);
  ALERT_STRIP.open = d.open;
  // tab title: blinks while alerts are open
  clearInterval(ALERT_STRIP.blink);
  if (d.open) {
    let on = false;
    ALERT_STRIP.blink = setInterval(() => { on = !on; document.title = on ? `(${d.open}) ⚠ ${d.critical ? 'ALERT' : 'Warning'}` : ALERT_STRIP.title; }, 1000);
  } else document.title = ALERT_STRIP.title;
  if (currentPage() === 'index.html') return;          // the Live page has its own banner
  let el = document.getElementById('alert-strip');
  if (!el) {
    const main = document.querySelector('main.main'); if (!main) return;
    main.insertAdjacentHTML('afterbegin', `<div class="alert-strip" id="alert-strip" role="alert" hidden></div>`);
    el = document.getElementById('alert-strip');
    el.addEventListener('click', async (e) => {
      if (e.target.closest('[data-strip-mute]')) {
        ALERT_STRIP.muted = !ALERT_STRIP.muted; localStorage.setItem('alert-muted', ALERT_STRIP.muted ? '1' : '');
        refreshAlertStrip(); if (!ALERT_STRIP.muted) beep(false); return;
      }
      const a = e.target.closest('[data-strip-ack]');
      if (a) { try { await apiPost(`/api/alerts/${a.dataset.stripAck}/ack`, {}); refreshAlertStrip(); } catch (err) { toast(err.message); } }
    });
  }
  if (!d.open) { el.hidden = true; return; }
  const t = new Date(newest.triggered_at.endsWith('Z') ? newest.triggered_at : newest.triggered_at + 'Z');
  el.className = `alert-strip ${d.critical ? 'crit' : ''}`;
  el.hidden = false;
  el.innerHTML = `<span class="as-icon">${icon('alert')}</span>
    <div class="as-main"><div class="as-title">${escapeHtml(newest.rule_name)} — ${escapeHtml(newest.message)}</div>
      <div class="as-sub">${escapeHtml(newest.camera)}${newest.zone ? ' · ' + escapeHtml(newest.zone) : ''} · ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}
        · ${d.open} open alert${d.open === 1 ? '' : 's'}${d.critical ? ` (${d.critical} critical)` : ''}</div></div>
    ${can('manage_alerts') ? `<button class="btn" data-strip-ack="${newest.id}">${icon('check')}Acknowledge</button>` : ''}
    <a class="btn primary" href="index.html">Open Live</a>
    <button class="icon-btn" data-strip-mute title="${ALERT_STRIP.muted ? 'Sound off — click to turn it on' : 'Sound on — click to mute'}">${ALERT_STRIP.muted ? '🔇' : '🔊'}</button>`;
}
document.addEventListener('DOMContentLoaded', () => accessReady.then(ok => {
  if (!ok || !can('live')) return;
  refreshAlertStrip();
  setInterval(() => { if (!document.hidden || ALERT_STRIP.open) refreshAlertStrip(); }, 4000);
}));
