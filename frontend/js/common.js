/* ────────────────────────────────────────────────────────────────────────────
 * COMMON HELPERS — used by all pages.
 * ──────────────────────────────────────────────────────────────────────────── */

const LONG_MIN = 10;     // a presence this long shows red (the default outside-the-wall dwell rule)
const WARN_MIN = 5;      // … and amber from here

/* ─── Formatters ─── */

function fmtTime(d) {
  d = (d instanceof Date) ? d : new Date(d);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtDate(d) {
  d = (d instanceof Date) ? d : new Date(d);
  return `${pad(d.getDate())} ${d.toLocaleString('default', { month: 'short' }).toUpperCase()} ${d.getFullYear()}`;
}
function fmtDuration(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${pad(h)}:${pad(m % 60)}:${pad(s)}`;
  }
  return `${pad(m)}:${pad(s)}`;
}
function fmtRelative(d) {
  d = (d instanceof Date) ? d : new Date(d);
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 5) return 'just now';
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
/* ─── Clock in the page header (#clock) ─── */
function startClock() {
  const tick = () => { const el = document.getElementById('clock'); if (el) el.textContent = `${fmtDate(new Date())} · ${fmtTime(new Date())}`; };
  tick(); setInterval(tick, 1000);
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

/* ─── Direct last-image modal for a presence row click ─── */
function openBadgeLastImage(session) {
  ensureModal();
  const modal = document.querySelector('.modal');
  modal.classList.remove('modal-detail');

  const badge = session.badge || '—';
  const lastCamera = session.last_camera || '—';
  const sightings = session.total_sightings || 0;

  // Duration (live)
  const firstDt = session.first_seen
    ? new Date(session.first_seen.endsWith('Z') ? session.first_seen : session.first_seen + 'Z')
    : null;
  const durationStr = firstDt
    ? fmtDuration((Date.now() - firstDt.getTime()) / 1000) + ' (live)'
    : '—';

  const lastDt = session.last_seen
    ? new Date(session.last_seen.endsWith('Z') ? session.last_seen : session.last_seen + 'Z')
    : null;
  const lastSeenStr = lastDt ? fmtTime(lastDt) : '—';

  document.getElementById('modal-title').textContent = badge;
  document.getElementById('modal-sub').textContent =
    `📷 ${lastCamera}  ·  last seen ${lastSeenStr}  ·  ${sightings} sighting${sightings !== 1 ? 's' : ''}  ·  duration ${durationStr}`;

  const body = document.getElementById('modal-body');
  if (session.has_image && session.last_image) {
    body.innerHTML = `
      <img class="modal-img" src="/images/${encodeURIComponent(session.last_image)}"
           alt="${escapeHtml(badge)}"
           onerror="this.outerHTML='<div class=&quot;modal-no-image&quot;>⊘ NO IMAGE AVAILABLE</div>'">`;
  } else {
    body.innerHTML = `<div class="modal-no-image">⊘ NO IMAGE AVAILABLE<br><br>image was cleaned up or never archived</div>`;
  }

  document.getElementById('modal-overlay').classList.add('open');
}
window.openBadgeLastImage = openBadgeLastImage;

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
      `<div class="modal-no-image">no presences found for this class</div>`;
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
    <rect x="32" y="10" width="14" height="20" rx="2" fill="hsl(${(hue + 60) % 360},55%,55%)" opacity="0.7"/>
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
  if (m >= LONG_MIN) return 'alert';
  if (m >= WARN_MIN) return 'warn';
  return 'ok';
}

/* ─── Header pills — breaches today / outside now (as WA's "At desk now"), shared by the perimeter pages ─── */
function updateTopbarPills(stats) {
  if (!stats) return;
  const b = stats.breaches_today || 0, w = stats.watching_outside || 0;
  const pb = document.getElementById('pill-breaches'), po = document.getElementById('pill-outside');
  const pill = (el, cls, label, n) => { if (!el) return; el.className = `live-now ${n ? cls : ''}`; el.hidden = false;
    el.innerHTML = `<span class="dot"></span><span class="ln-text"><span class="ln-label">${label}</span><b>${n}</b></span>`; };
  pill(pb, 'bad', 'Breaches today:', b);
  pill(po, 'warn', 'Outside now:', w);
}
window.updateTopbarPills = updateTopbarPills;

/* ─── Bootstrap (each perimeter page): the Workplace Analytics shell (shell.js), the image viewer, header pills ─── */
function bootstrap() {
  accessReady.then(ok => {
    if (!ok) return;
    renderShell();
    ensureModal();
    startClock();
    if (document.getElementById('pill-breaches')) {
      const refresh = () => getStats().then(updateTopbarPills).catch(() => { });
      refresh(); setInterval(refresh, 15000);
    }
  });
}

const copyText = copyToClipboard;                    // shell.js (Workplace Analytics)

/** the Workplace Analytics pager's markup for a list paged by the page itself (page 0-based; onclick = fnName(page)) */
function waPagerHtml(page, total, size, fnName) {
  if (total <= size) return total ? `<span class="pg-info">${total} ${total === 1 ? 'entry' : 'entries'}</span>` : '';
  const pages = Math.ceil(total / size), cur = page + 1;
  const a = page * size + 1, b = Math.min(total, cur * size);
  const nums = [];
  for (let p = 1; p <= pages; p++) {
    if (p === 1 || p === pages || Math.abs(p - cur) <= 1) nums.push(p);
    else if (nums[nums.length - 1] !== '…') nums.push('…');
  }
  const btn = (p, label, aria, on) => `<button type="button" class="pg-btn${on ? ' on' : ''}" onclick="${fnName}(${p - 1})"${p < 1 || p > pages || on ? ' disabled' : ''} aria-label="${aria}">${label}</button>`;
  return `<span class="pg-info">${a}–${b} of ${total}</span><span class="pg-nav">` + btn(cur - 1, '‹', 'Previous page')
    + nums.map(p => p === '…' ? '<span class="pg-gap">…</span>' : btn(p, p, `Page ${p}`, p === cur)).join('') + btn(cur + 1, '›', 'Next page') + '</span>';
}
