/* ────────────────────────────────────────────────────────────────────────────
 * COMMON HELPERS — used by all pages.
 * ──────────────────────────────────────────────────────────────────────────── */

const LOITER_MIN = 15;   // loitering threshold (minutes)
const WARN_MIN = 5;      // approaching threshold (minutes)

/* ─── Formatters ─── */
function pad(n) { return String(n).padStart(2, '0'); }

function fmtTime(d) {
  d = (d instanceof Date) ? d : new Date(d);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtDate(d) {
  d = (d instanceof Date) ? d : new Date(d);
  return `${pad(d.getDate())} ${d.toLocaleString('default',{month:'short'}).toUpperCase()} ${d.getFullYear()}`;
}
function fmtDuration(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${pad(h)}:${pad(m%60)}:${pad(s)}`;
  }
  return `${pad(m)}:${pad(s)}`;
}
function fmtRelative(d) {
  d = (d instanceof Date) ? d : new Date(d);
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 5) return 'just now';
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec/3600)}h ago`;
  return `${Math.floor(sec/86400)}d ago`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

/* ─── Topbar HTML — injected by each page ─── */
const TRINETRA_LOGO_SVG = `
<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="12" cy="5.5" rx="3.2" ry="2.2" fill="#fff" opacity="0.95"/>
  <circle cx="12" cy="5.5" r="1.1" fill="#0a0e17"/>
  <ellipse cx="5.5" cy="16.5" rx="3.2" ry="2.2" fill="#fff" opacity="0.95"/>
  <circle cx="5.5" cy="16.5" r="1.1" fill="#0a0e17"/>
  <ellipse cx="18.5" cy="16.5" rx="3.2" ry="2.2" fill="#fff" opacity="0.95"/>
  <circle cx="18.5" cy="16.5" r="1.1" fill="#0a0e17"/>
  <path d="M 12 8 L 5.5 14.5 M 12 8 L 18.5 14.5 M 8.5 16.5 L 15.5 16.5"
        stroke="#fff" stroke-width="0.8" opacity="0.4" stroke-linecap="round"/>
</svg>`;

function renderTopbar(activeTab) {
  const stats = dummyStats();
  const tab = (id, label, href) => `
    <a href="${href}" class="tab ${id===activeTab?'active':''}">${label}</a>`;
  return `
<div class="topbar">
  <div class="brand">
    <div class="logo">${TRINETRA_LOGO_SVG}</div>
    <div class="brand-name">TRINETRA SYSTEMS<span>monitor</span></div>
  </div>
  <div class="tabs">
    ${tab('live', 'Live', 'index.html')}
    ${tab('logs', 'Logs & Charts', 'logs.html')}
    ${tab('visits', 'Visits', 'visits.html')}
    ${tab('wl',   'Whitelist', 'whitelist.html')}
  </div>
  <div class="status-cluster">
    <div class="status-pill"><span class="dot"></span><span>${stats.jetsons_online} / ${stats.jetsons_total} JETSONS ONLINE</span></div>
    <div class="status-pill ${stats.loitering_count?'alert':''}"><span class="dot ${stats.loitering_count?'red':''}"></span><span>${stats.loitering_count} LOITERING</span></div>
    <div class="status-pill"><span class="dot"></span><span>${stats.active_sessions} IN PREMISES</span></div>
  </div>
  <div class="topbar-right">
    <div class="time-stamp"><b id="clock">${fmtDate(NOW)} · ${fmtTime(NOW)}</b></div>
    <a href="admin.html" class="icon-btn" title="Admin Panel" style="text-decoration:none;font-size:15px;">⚙</a>
    <button class="icon-btn" id="theme-toggle" title="Toggle theme">☾</button>
    <button class="icon-btn" onclick="location.reload()" title="Refresh">↻</button>
  </div>
</div>`;
}

/* ─── Clock — ticks every second ─── */
function startClock() {
  setInterval(() => {
    const el = document.getElementById('clock');
    if (el) {
      const d = new Date();
      el.textContent = `${fmtDate(d)} · ${fmtTime(d)}`;
    }
  }, 1000);
}

/* ─── Theme toggle — persisted in localStorage ─── */
function applyTheme(theme) {
  document.body.setAttribute('data-theme', theme);
  localStorage.setItem('trinetra-theme', theme);
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = theme === 'dark' ? '☾' : '☀';
}
function initTheme() {
  const saved = localStorage.getItem('trinetra-theme') || 'dark';
  applyTheme(saved);
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.addEventListener('click', () => {
    applyTheme(document.body.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  });
}

/* ─── Image modal ─── */
function ensureModal() {
  if (document.getElementById('modal-overlay')) return;
  const html = `
    <div id="modal-overlay" class="modal-overlay" onclick="closeModal(event)">
      <div class="modal" onclick="event.stopPropagation()">
        <div class="modal-header">
          <div>
            <h2 id="modal-title">—</h2>
            <div class="sub" id="modal-sub">—</div>
          </div>
          <button class="modal-close" onclick="closeModal()">✕ CLOSE</button>
        </div>
        <div class="modal-body" id="modal-body">—</div>
      </div>
    </div>`;
  document.body.insertAdjacentHTML('beforeend', html);
}
function openModal({ title, sub, filename, hasImage = true }) {
  ensureModal();
  // Remove detail-mode class if present from previous open
  document.querySelector('.modal')?.classList.remove('modal-detail');
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-sub').textContent = sub;
  const body = document.getElementById('modal-body');
  if (hasImage) {
    // Try the real archive image first; fall back to a placeholder SVG on error
    body.innerHTML = `<img class="modal-img" src="/images/${encodeURIComponent(filename)}"
                           alt="${escapeHtml(title)}"
                           onerror="this.outerHTML='<div class=&quot;modal-no-image&quot;>⊘ NO IMAGE AVAILABLE</div>'">`;
  } else {
    body.innerHTML = `<div class="modal-no-image">⊘ NO IMAGE AVAILABLE<br><br>image was cleaned up or never archived</div>`;
  }
  document.getElementById('modal-overlay').classList.add('open');
}

/* ─── Badge detail modal — sessions timeline (handoff-aware) ─── */
async function openBadgeDetail(badge, lastCamera) {
  ensureModal();
  const modal = document.querySelector('.modal');
  modal.classList.add('modal-detail');
  document.getElementById('modal-title').textContent = badge;
  document.getElementById('modal-sub').textContent = 'loading sessions…';
  document.getElementById('modal-body').innerHTML =
    `<div class="modal-no-image" style="padding: 40px;">loading…</div>`;
  document.getElementById('modal-overlay').classList.add('open');

  try {
    const sessions = await getBadgeSessions(badge);
    renderBadgeSessionsList(badge, sessions);
  } catch (e) {
    document.getElementById('modal-sub').textContent = 'failed to load';
    document.getElementById('modal-body').innerHTML =
      `<div class="modal-no-image">error: ${escapeHtml(e.message)}</div>`;
  }
}

/* List view — shows every session (= one person-visit) for the badge */
function renderBadgeSessionsList(badge, sessions) {
  if (sessions.length === 0) {
    document.getElementById('modal-sub').textContent = 'no sessions';
    document.getElementById('modal-body').innerHTML =
      `<div class="modal-no-image">no sessions found for this badge</div>`;
    return;
  }

  const liveCount = sessions.filter(s => !s.closed).length;
  const totalCount = sessions.length;
  document.getElementById('modal-sub').textContent =
    `${totalCount} session${totalCount > 1 ? 's' : ''} · ${liveCount} active` +
    ` · each session = one person-visit (badge may have been handed off)`;

  let html = `<div class="modal-log-list-header">All sessions for ${escapeHtml(badge)}</div>`;
  html += `<div class="modal-log-list" style="max-height: 480px;">`;
  sessions.forEach((s, i) => {
    const startDt = new Date(s.first_seen.endsWith('Z') ? s.first_seen : s.first_seen + 'Z');
    const endDt = new Date((s.closed_at || s.last_seen).endsWith('Z') ? (s.closed_at || s.last_seen) : (s.closed_at || s.last_seen) + 'Z');

    let durationLabel;
    if (s.closed && s.duration_seconds != null) {
      durationLabel = fmtDuration(s.duration_seconds);
    } else if (!s.closed) {
      const live = (Date.now() - startDt.getTime()) / 1000;
      durationLabel = `${fmtDuration(live)} (live)`;
    } else {
      durationLabel = '—';
    }

    const liveBadge = s.closed ? '' :
      `<span class="status-tag alert" style="margin-left: 6px;">LIVE</span>`;
    const alertBadge = s.had_alert ?
      `<span class="status-tag warn" style="margin-left: 4px;">⚠ ALERT</span>` : '';
    const endTimeLabel = s.closed ?
      `${fmtTime(endDt)} (${escapeHtml(s.last_camera || 'exit')})` :
      `ongoing`;

    html += `
      <div class="modal-log-item" onclick="openSessionDetail(${s.id}, '${escapeHtml(badge)}')">
        <div class="info">
          <div class="time">
            #${i + 1} · ${fmtTime(startDt)} → ${endTimeLabel}${liveBadge}${alertBadge}
          </div>
          <div class="meta">
            duration ${durationLabel} · ${s.total_sightings} sighting${s.total_sightings > 1 ? 's' : ''} ·
            <span style="color: var(--text-dim)">click to see this visit's detections</span>
          </div>
        </div>
      </div>`;
  });
  html += `</div>`;
  document.getElementById('modal-body').innerHTML = html;
}

/* Detail view — drill into one session's detections */
async function openSessionDetail(sessionId, badge) {
  document.getElementById('modal-sub').textContent = `session #${sessionId} · loading…`;
  document.getElementById('modal-body').innerHTML =
    `<div class="modal-no-image" style="padding: 40px;">loading…</div>`;

  try {
    const detections = await getSessionDetections(sessionId);
    renderSessionDetections(badge, sessionId, detections);
  } catch (e) {
    document.getElementById('modal-body').innerHTML =
      `<div class="modal-no-image">error: ${escapeHtml(e.message)}</div>`;
  }
}

function renderSessionDetections(badge, sessionId, detections) {
  if (detections.length === 0) {
    document.getElementById('modal-body').innerHTML =
      `<div class="modal-no-image">no detections in this session</div>`;
    return;
  }

  // Detections were returned ASC — reverse to newest-first for display
  const sorted = [...detections].reverse();
  const first = sorted[0];
  const firstTs = new Date(first.timestamp.endsWith('Z') ? first.timestamp : first.timestamp + 'Z');
  document.getElementById('modal-sub').textContent =
    `session #${sessionId} · ${first.camera} · ${fmtTime(firstTs)} · ${first.type.toUpperCase()}`;

  let imageHTML;
  if (first.has_image) {
    imageHTML = `<img class="modal-img" id="modal-main-img"
                      src="/images/${encodeURIComponent(first.filename)}"
                      alt="${escapeHtml(badge)}"
                      onerror="this.outerHTML='<div class=&quot;modal-no-image&quot; id=&quot;modal-main-img&quot;>⊘ NO IMAGE AVAILABLE</div>'">`;
  } else {
    imageHTML = `<div class="modal-no-image" id="modal-main-img">⊘ NO IMAGE AVAILABLE</div>`;
  }

  const backBtn = `<button class="ack-btn" style="margin-bottom: 12px;"
                     onclick="openBadgeDetail('${escapeHtml(badge)}', '')">← all sessions</button>`;

  let logHTML = `<div class="modal-log-list-header">Detections in session #${sessionId} (${detections.length})</div>`;
  logHTML += `<div class="modal-log-list">`;
  sorted.forEach((d, i) => {
    const ts = new Date(d.timestamp.endsWith('Z') ? d.timestamp : d.timestamp + 'Z');
    const isToday = ts.toDateString() === new Date().toDateString();
    const active = i === 0 ? 'active' : '';
    logHTML += `
      <div class="modal-log-item ${active}" onclick="selectBadgeDetailLog(${i})">
        <div class="thumb">${thumbSVG(d.badge)}</div>
        <div class="info">
          <div class="time">${fmtTime(ts)}${isToday ? '' : ` <span style="color:var(--text-dim);font-weight:400">· ${fmtDate(ts)}</span>`}</div>
          <div class="meta">${escapeHtml(d.camera)} · <span class="tag-${d.type}">${d.type.toUpperCase()}</span></div>
        </div>
      </div>`;
  });
  logHTML += `</div>`;

  document.getElementById('modal-body').innerHTML =
    `<div style="padding: 16px 24px;">${backBtn}${imageHTML}</div>${logHTML}`;
  window._currentBadgeHistory = sorted;
}

function selectBadgeDetailLog(index) {
  const history = window._currentBadgeHistory;
  if (!history || !history[index]) return;
  const d = history[index];
  const ts = new Date(d.timestamp.endsWith('Z') ? d.timestamp : d.timestamp + 'Z');

  document.getElementById('modal-sub').textContent =
    `${d.camera} · ${fmtTime(ts)} · ${d.type.toUpperCase()}`;

  const oldImg = document.getElementById('modal-main-img');
  if (!oldImg) return;
  let newHTML;
  if (d.has_image) {
    newHTML = `<img class="modal-img" id="modal-main-img"
                    src="/images/${encodeURIComponent(d.filename)}"
                    alt="${escapeHtml(d.badge)}"
                    onerror="this.outerHTML='<div class=&quot;modal-no-image&quot; id=&quot;modal-main-img&quot;>⊘ NO IMAGE AVAILABLE</div>'">`;
  } else {
    newHTML = `<div class="modal-no-image" id="modal-main-img">⊘ NO IMAGE AVAILABLE</div>`;
  }
  oldImg.outerHTML = newHTML;

  document.querySelectorAll('.modal-log-item').forEach((el, i) => {
    el.classList.toggle('active', i === index);
  });
}

// Pseudo-thumbnail SVG generator (deterministic colors per badge)
function thumbSVG(badge) {
  const hash = [...badge].reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0);
  const hue = Math.abs(hash) % 360;
  return `<svg viewBox="0 0 56 36" preserveAspectRatio="none" style="width:100%;height:100%;">
    <rect width="56" height="36" fill="hsl(${hue},25%,28%)" opacity="0.4"/>
    <circle cx="22" cy="18" r="6" fill="hsl(${hue},65%,55%)" opacity="0.9"/>
    <rect x="32" y="10" width="14" height="20" rx="2" fill="hsl(${(hue+60)%360},55%,55%)" opacity="0.7"/>
  </svg>`;
}

window.openBadgeDetail = openBadgeDetail;
window.openSessionDetail = openSessionDetail;
window.selectBadgeDetailLog = selectBadgeDetailLog;
function closeModal(e) {
  if (e && e.target.id !== 'modal-overlay' && e.target.className !== 'modal-close') return;
  document.getElementById('modal-overlay').classList.remove('open');
}
window.closeModal = closeModal;

/* ─── Status helpers ─── */
function durationClass(secs) {
  const m = secs / 60;
  if (m >= LOITER_MIN) return 'alert';
  if (m >= WARN_MIN)   return 'warn';
  return 'ok';
}
function statusForSession(session) {
  const secs = (Date.now() - session.first_seen.getTime()) / 1000;
  if (session.whitelisted) return { cls: 'wl', label: 'WHITELIST' };
  if (secs / 60 >= LOITER_MIN) return { cls: 'alert', label: 'LOITERING' };
  if (secs / 60 >= WARN_MIN)   return { cls: 'warn',  label: 'APPROACHING' };
  return { cls: 'ok', label: 'OK' };
}

/* ─── Bootstrap (call from each page) ─── */
function bootstrap(activeTab) {
  document.body.insertAdjacentHTML('afterbegin', renderTopbar(activeTab));
  ensureModal();
  startClock();
  initTheme();
}
