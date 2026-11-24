/* ────────────────────────────────────────────────────────────────────────────
 * WHITELIST PAGE — calls real API via api.js
 * Badge dropdown is populated from classes.txt (via /api/badges)
 * ──────────────────────────────────────────────────────────────────────────── */

let whitelist = [];
let allBadges = [];

async function refreshAll() {
  try {
    const [wl, badges] = await Promise.all([getWhitelist(), getBadges()]);
    whitelist = wl;
    allBadges = badges.badges || [];
    renderDropdown();
    renderList();
  } catch (e) {
    console.error('refresh failed:', e);
  }
}

function renderDropdown() {
  const sel = document.getElementById('wl-badge');
  const whitelisted = new Set(whitelist.map(w => w.badge));
  const available = allBadges.filter(b => !whitelisted.has(b.badge));

  if (available.length === 0) {
    sel.innerHTML = `<option value="">— all badges are whitelisted —</option>`;
    sel.disabled = true;
    return;
  }

  sel.disabled = false;
  sel.innerHTML = `<option value="">— select a badge —</option>` +
    available.map(b => {
      const tag = b.auto_discovered ? ' (auto)' : '';
      return `<option value="${escapeHtml(b.badge)}">${escapeHtml(b.badge)}${tag}</option>`;
    }).join('');
}

function renderList() {
  const container = document.getElementById('wl-list');
  if (whitelist.length === 0) {
    container.innerHTML = `<div style="text-align:center;color:var(--text-dim);padding:30px;font-size:14px;">no whitelisted badges</div>`;
    return;
  }
  container.innerHTML = whitelist.map(w => {
    const added = new Date(w.added_at.endsWith('Z') ? w.added_at : w.added_at + 'Z');
    return `
      <div class="wl-row">
        <div>
          <span class="label">${escapeHtml(w.badge)}</span>
          <span class="reason">${escapeHtml(w.reason || '')}</span>
        </div>
        <div style="display:flex;align-items:center;">
          <span class="added">added ${fmtDate(added)}</span>
          <button class="remove" data-badge="${escapeHtml(w.badge)}">remove</button>
        </div>
      </div>`;
  }).join('');
}

async function onAdd() {
  const badgeSel = document.getElementById('wl-badge');
  const reasonInput = document.getElementById('wl-reason');
  const badge = badgeSel.value.trim();
  const reason = reasonInput.value.trim();
  if (!badge) {
    badgeSel.focus();
    return;
  }
  try {
    await addWhitelist(badge, reason);
    badgeSel.value = '';
    reasonInput.value = '';
    await refreshAll();
  } catch (e) {
    alert('Add failed: ' + e.message);
  }
}

async function onRemove(badge) {
  if (!confirm(`Remove ${badge} from whitelist?`)) return;
  try {
    await removeWhitelist(badge);
    await refreshAll();
  } catch (e) {
    alert('Remove failed: ' + e.message);
  }
}
window.onRemove = onRemove;

document.addEventListener('DOMContentLoaded', async () => {
  bootstrap('wl');
  // Remove buttons carry the badge in a data attribute — no value is ever
  // written into an inline event handler.
  document.getElementById('wl-list').addEventListener('click', e => {
    const btn = e.target.closest('button.remove[data-badge]');
    if (btn) onRemove(btn.dataset.badge);
  });
  // Whitelisting silences alerts: only those who may acknowledge alerts change the list.
  getMe().then(me => {
    if (!canSee(me, 'manage_alerts')) {
      document.querySelector('.admin-input').style.display = 'none';
      document.getElementById('wl-list').classList.add('read-only');
    }
  }).catch(() => { });
  await refreshAll();
  document.getElementById('wl-add-btn').addEventListener('click', onAdd);
  document.getElementById('wl-reason').addEventListener('keydown', e => {
    if (e.key === 'Enter') onAdd();
  });
});
