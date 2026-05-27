/* ────────────────────────────────────────────────────────────────────────────
 * VISITS PAGE — one row per visit (session), click for full timeline modal
 * ──────────────────────────────────────────────────────────────────────────── */

const visitFilters = { search: '', status: 'all', badge: '', date: '' };
let cachedVisits = [];

async function refreshAll() {
  try {
    cachedVisits = await getVisits({
      status: visitFilters.status,
      badge: visitFilters.badge || undefined,
      date: visitFilters.date || undefined,
      limit: 500,
    });
    renderVisitsTable();
  } catch (e) {
    console.error('visits fetch failed:', e);
    document.getElementById('visits-body').innerHTML =
      `<tr><td colspan="8" style="text-align:center;padding:30px;color:var(--red);">failed to load: ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function populateBadgeDropdown() {
  const sel = document.getElementById('visit-filter-badge');
  if (sel.options.length > 1) return;
  try {
    const b = await getBadges();
    sel.innerHTML = `<option value="">All badges</option>` +
      (b.badges || []).map(x => `<option value="${escapeHtml(x.badge)}">${escapeHtml(x.badge)}</option>`).join('');
  } catch (e) { console.error('badges fetch failed:', e); }
}

function renderVisitsTable() {
  const tbody = document.getElementById('visits-body');
  // Apply local search filter (server doesn't filter on free text for visits)
  let visits = cachedVisits;
  if (visitFilters.search) {
    const q = visitFilters.search.toLowerCase();
    visits = visits.filter(v =>
      v.badge.toLowerCase().includes(q) ||
      (v.last_camera || '').toLowerCase().includes(q) ||
      Object.keys(v.cameras || {}).some(c => c.toLowerCase().includes(q))
    );
  }

  document.getElementById('visits-meta').textContent =
    `showing ${visits.length} of ${cachedVisits.length}`;

  if (visits.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:30px;color:var(--text-dim);">no visits match filters</td></tr>`;
    return;
  }

  tbody.innerHTML = visits.map((v, idx) => {
    const first = new Date(v.first_seen.endsWith('Z') ? v.first_seen : v.first_seen + 'Z');
    const last  = new Date(v.last_seen.endsWith('Z')  ? v.last_seen  : v.last_seen  + 'Z');
    const duration = v.duration_seconds;

    const sameDay = first.toDateString() === last.toDateString();
    const enteredStr = sameDay ? fmtTime(first) : `${fmtDate(first)} ${fmtTime(first)}`;
    const exitedStr  = v.closed
      ? (sameDay ? fmtTime(last) : `${fmtDate(last)} ${fmtTime(last)}`)
      : `<span style="color:var(--text-dim)">still here</span>`;

    const dCls = durationClass(duration);
    const camsBadges = Object.entries(v.cameras || {})
      .sort((a, b) => b[1] - a[1])
      .map(([cam, count]) =>
        `<span class="cam-chip">${escapeHtml(cam)} <span class="cam-chip-count">${count}</span></span>`
      ).join('');

    const statusTag = v.closed
      ? '<span class="status-tag" style="background:rgba(155,138,255,0.15);color:var(--violet);border:1px solid var(--violet);">COMPLETED</span>'
      : (v.whitelisted
        ? '<span class="status-tag wl">WHITELIST</span>'
        : duration / 60 >= LOITER_MIN
          ? '<span class="status-tag alert">LOITERING</span>'
          : '<span class="status-tag ok">IN PREMISES</span>');

    return `
      <tr class="clickable" onclick="showVisitTimeline(${v.id})">
        <td class="mono"><span style="color:var(--text-dim)">#${v.id}</span></td>
        <td class="mono"><b>${escapeHtml(v.badge)}</b></td>
        <td class="mono">${enteredStr}</td>
        <td class="mono">${exitedStr}</td>
        <td><span class="duration-pill ${dCls}">${fmtDuration(duration)}</span></td>
        <td class="mono">${v.total_sightings}</td>
        <td>${camsBadges}</td>
        <td>${statusTag}</td>
      </tr>`;
  }).join('');
}

/* ─── Visit timeline modal ─── */
async function showVisitTimeline(visitId) {
  ensureModal();
  const modal = document.querySelector('.modal');
  modal.classList.add('modal-detail');
  document.getElementById('modal-title').textContent = `Visit #${visitId}`;
  document.getElementById('modal-sub').textContent = 'loading timeline…';
  document.getElementById('modal-body').innerHTML =
    `<div class="modal-no-image" style="padding:40px;">loading…</div>`;
  document.getElementById('modal-overlay').classList.add('open');

  try {
    const data = await getVisitTimeline(visitId);
    renderVisitTimelineModal(data);
  } catch (e) {
    document.getElementById('modal-body').innerHTML =
      `<div class="modal-no-image">failed: ${escapeHtml(e.message)}</div>`;
  }
}
window.showVisitTimeline = showVisitTimeline;

function renderVisitTimelineModal(data) {
  const visit = data.visit;
  const detections = data.detections || [];

  const first = new Date(visit.first_seen.endsWith('Z') ? visit.first_seen : visit.first_seen + 'Z');
  const last  = new Date(visit.last_seen.endsWith('Z')  ? visit.last_seen  : visit.last_seen  + 'Z');
  const duration = Math.floor((last - first) / 1000);

  document.getElementById('modal-title').innerHTML =
    `${escapeHtml(visit.badge)} <span style="color:var(--text-dim);font-weight:400;font-size:14px;">Visit #${visit.id}</span>`;
  document.getElementById('modal-sub').innerHTML =
    `${fmtDate(first)} · ${fmtTime(first)} → ${visit.closed ? fmtTime(last) : '<span style="color:var(--cyan)">still here</span>'} · ${fmtDuration(duration)} · ${visit.total_sightings} sightings`;

  // First detection image area
  const first_det = detections[0];
  let imageHTML = `<div class="modal-no-image" id="visit-main-img">no detections in this visit</div>`;
  if (first_det) {
    if (first_det.has_image) {
      imageHTML = `<img class="modal-img" id="visit-main-img"
                        src="/images/${encodeURIComponent(first_det.filename)}"
                        alt="${escapeHtml(visit.badge)}"
                        onerror="this.outerHTML='<div class=&quot;modal-no-image&quot; id=&quot;visit-main-img&quot;>⊘ NO IMAGE AVAILABLE</div>'">`;
    } else {
      imageHTML = `<div class="modal-no-image" id="visit-main-img">⊘ NO IMAGE AVAILABLE</div>`;
    }
  }

  // Timeline list — oldest first reads naturally as the visit unfolded
  let logHTML = `<div class="modal-log-list-header">Timeline (${detections.length} events) · earliest first</div>`;
  logHTML += `<div class="modal-log-list">`;
  if (detections.length === 0) {
    logHTML += `<div class="notif-empty">no detection records for this visit</div>`;
  } else {
    detections.forEach((d, i) => {
      const ts = new Date(d.timestamp.endsWith('Z') ? d.timestamp : d.timestamp + 'Z');
      const active = i === 0 ? 'active' : '';
      logHTML += `
        <div class="modal-log-item ${active}" onclick="selectVisitLog(${i})">
          <div class="thumb">${thumbSVG(d.badge)}</div>
          <div class="info">
            <div class="time">${fmtTime(ts)}</div>
            <div class="meta">${escapeHtml(d.camera)} · <span class="tag-${d.type}">${d.type.toUpperCase()}</span></div>
          </div>
        </div>`;
    });
  }
  logHTML += `</div>`;

  document.getElementById('modal-body').innerHTML =
    `<div style="padding:16px 24px;">${imageHTML}</div>${logHTML}`;
  window._currentVisitTimeline = detections;
}

function selectVisitLog(index) {
  const detections = window._currentVisitTimeline;
  if (!detections || !detections[index]) return;
  const d = detections[index];
  const ts = new Date(d.timestamp.endsWith('Z') ? d.timestamp : d.timestamp + 'Z');

  const oldImg = document.getElementById('visit-main-img');
  const newHTML = d.has_image
    ? `<img class="modal-img" id="visit-main-img"
            src="/images/${encodeURIComponent(d.filename)}"
            alt="${escapeHtml(d.badge)}"
            onerror="this.outerHTML='<div class=&quot;modal-no-image&quot; id=&quot;visit-main-img&quot;>⊘ NO IMAGE AVAILABLE</div>'">`
    : `<div class="modal-no-image" id="visit-main-img">⊘ NO IMAGE AVAILABLE</div>`;
  oldImg.outerHTML = newHTML;

  document.querySelectorAll('.modal-log-item').forEach((el, i) => {
    el.classList.toggle('active', i === index);
  });
}
window.selectVisitLog = selectVisitLog;

/* ─── Init ─── */
function initFilters() {
  document.getElementById('visit-filter-search').addEventListener('input', e => {
    visitFilters.search = e.target.value;
    renderVisitsTable();
  });
  document.getElementById('visit-filter-status').addEventListener('change', e => {
    visitFilters.status = e.target.value;
    refreshAll();
  });
  document.getElementById('visit-filter-badge').addEventListener('change', e => {
    visitFilters.badge = e.target.value;
    refreshAll();
  });
  document.getElementById('visit-filter-date').addEventListener('change', e => {
    visitFilters.date = e.target.value;
    refreshAll();
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  bootstrap('visits');
  initFilters();
  await Promise.all([refreshAll(), populateBadgeDropdown()]);
  setInterval(refreshAll, 10000);
});
