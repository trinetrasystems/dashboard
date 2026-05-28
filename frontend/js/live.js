/* ────────────────────────────────────────────────────────────────────────────
 * LIVE MONITORING PAGE — calls real API via api.js
 * Adds: Live Notifications panel + enhanced premises modal (image + history)
 * ──────────────────────────────────────────────────────────────────────────── */

const NOTIF_MAX = 50;
let activeSessions = [];
let cachedStats = null;
let cachedNotifications = [];

async function refreshAll() {
  try {
    const [sessions, stats, recentDetections] = await Promise.all([
      getSessions(),
      getStats(),
      getDetections({}),   // today's detections, newest first
    ]);
    activeSessions = sessions.map(s => ({
      ...s,
      first_seen_dt: new Date(s.first_seen.endsWith('Z') ? s.first_seen : s.first_seen + 'Z'),
      last_seen_dt: new Date(s.last_seen.endsWith('Z') ? s.last_seen : s.last_seen + 'Z'),
    }));
    cachedStats = stats;
    cachedNotifications = (recentDetections || []).slice(0, NOTIF_MAX);

    // Topbar status pills
    const pills = document.querySelectorAll('.status-pill');
    if (pills.length >= 3 && stats) {
      pills[0].innerHTML = `<span class="dot"></span><span>${stats.jetsons_online} / ${stats.jetsons_total} JETSONS ONLINE</span>`;
      const loiterCls = stats.loitering_count ? 'alert' : '';
      pills[1].className = `status-pill ${loiterCls}`;
      pills[1].innerHTML = `<span class="dot ${stats.loitering_count ? 'red' : ''}"></span><span>${stats.loitering_count} LOITERING</span>`;
      pills[2].innerHTML = `<span class="dot"></span><span>${stats.active_sessions} IN PREMISES</span>`;
    }

    renderLoiterBanner();
    renderKPIs();
    renderTodayChart();
    renderPremisesTable();
    renderNotifications();
  } catch (e) {
    console.error('refresh failed:', e);
  }
}

function renderLoiterBanner() {
  const banner = document.getElementById('loiter-banner');
  const list = document.getElementById('loiter-list');
  // Show ALL sessions loitering by time — both whitelisted and non-whitelisted.
  // Whitelisted ones get a Remove WL button; others get an Ack button.
  const loitering = activeSessions.filter(s => {
    const mins = (Date.now() - s.first_seen_dt.getTime()) / 60000;
    return mins >= LOITER_MIN;
  });
  if (loitering.length === 0) { banner.style.display = 'none'; return; }
  banner.style.display = 'block';
  list.innerHTML = loitering.map(s => {
    const secs = (Date.now() - s.first_seen_dt.getTime()) / 1000;
    const metaText = s.whitelisted
      ? `whitelisted · since ${fmtTime(s.first_seen_dt)} · ${escapeHtml(s.last_camera || '')}`
      : `since ${fmtTime(s.first_seen_dt)} · ${escapeHtml(s.last_camera || '')}`;
    const actionBtn = s.whitelisted
      ? `<button class="ack-btn" style="background:rgba(255,80,80,0.15);border-color:var(--red);color:var(--red);"
             onclick="removeFromWhitelist('${escapeHtml(s.badge)}')"
             title="Remove from whitelist — will be monitored for loitering again">
             ✕ Remove WL</button>`
      : (s.alert_id
          ? `<button class="ack-btn" onclick="ackAlert(${s.alert_id})" title="Acknowledge — adds badge to whitelist">
               ✓ Ack</button>`
          : '');
    return `
      <div class="loiter-item ${s.whitelisted ? 'acked' : ''}">
        <div class="info">
          <div class="label">${escapeHtml(s.badge)}</div>
          <div class="meta">${metaText}</div>
        </div>
        <div class="duration" data-since="${s.first_seen_dt.getTime()}">${fmtDuration(secs)}</div>
        ${actionBtn}
      </div>`;
  }).join('');
}

async function ackAlert(alertId) {
  try { await ackAlertAPI(alertId); await refreshAll(); }
  catch (e) { console.error('ack failed:', e); }
}
window.ackAlert = ackAlert;

function renderKPIs() {
  if (!cachedStats) return;
  document.getElementById('kpi-total').textContent = cachedStats.total_today;
  document.getElementById('kpi-active').textContent = cachedStats.active_sessions;
  document.getElementById('kpi-loiter').textContent = cachedStats.loitering_count;
}

function renderTodayChart() {
  if (!cachedStats) return;
  const data = cachedStats.hourly_today || new Array(24).fill(0);
  const max = Math.max(...data, 1);
  const barW = 32, gap = 10, x0 = 50, chartH = 180;
  const chartW = x0 + 24 * (barW + gap);
  let bars = '', labels = '';
  for (let i = 0; i < 24; i++) {
    const h = (data[i] / max) * (chartH - 30);
    const x = x0 + i * (barW + gap);
    const y = chartH - h;
    bars += `<rect x="${x}" y="${y}" width="${barW}" height="${h}" rx="3" fill="var(--cyan)" opacity="0.85"/>`;
    if (i % 4 === 0) {
      labels += `<text x="${x + barW / 2}" y="${chartH + 18}" text-anchor="middle" font-family="JetBrains Mono" font-size="10" fill="var(--text-dim)">${pad(i)}:00</text>`;
    }
  }
  const peakHr = data.indexOf(max);
  document.getElementById('chart-meta').textContent =
    max === 0 ? 'no detections yet today' : `peak ${pad(peakHr)}:00 · ${max} detections`;
  document.getElementById('today-chart').innerHTML = `
    <svg viewBox="0 0 ${chartW} 220" width="100%" preserveAspectRatio="none" style="display: block;">
      <line x1="${x0}" y1="${chartH}" x2="${chartW - 20}" y2="${chartH}" stroke="var(--border)"/>
      <line x1="${x0}" y1="${chartH * 0.5}" x2="${chartW - 20}" y2="${chartH * 0.5}" stroke="var(--border)" stroke-dasharray="2 4" opacity="0.5"/>
      ${bars}${labels}
      <text x="40" y="${chartH + 4}" text-anchor="end" font-family="JetBrains Mono" font-size="10" fill="var(--text-dim)">0</text>
      <text x="40" y="${chartH * 0.5 + 4}" text-anchor="end" font-family="JetBrains Mono" font-size="10" fill="var(--text-dim)">${Math.floor(max / 2)}</text>
      <text x="40" y="10" text-anchor="end" font-family="JetBrains Mono" font-size="10" fill="var(--text-dim)">${max}</text>
    </svg>`;
}

function renderPremisesTable() {
  const sorted = [...activeSessions].sort((a, b) => a.first_seen_dt - b.first_seen_dt);
  const tbody = document.getElementById('premises-body');
  if (sorted.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--text-dim);">no active sessions</td></tr>`;
    return;
  }
  tbody.innerHTML = sorted.map(s => {
    const secs = (Date.now() - s.first_seen_dt.getTime()) / 1000;
    const dCls = durationClass(secs);
    let status;
    if (s.whitelisted) status = { cls: 'wl', label: 'WHITELIST' };
    else if (secs / 60 >= LOITER_MIN) status = { cls: 'alert', label: 'LOITERING' };
    else if (secs / 60 >= WARN_MIN) status = { cls: 'warn', label: 'APPROACHING' };
    else status = { cls: 'ok', label: 'OK' };
    const rowCls = (!s.whitelisted && status.cls === 'alert') ? 'alert-row'
      : (!s.whitelisted && status.cls === 'warn') ? 'warn-row' : '';
    // Action button: Ack for LOITERING rows, Remove WL for WHITELIST rows
    let actionBtn = '';
    if (s.whitelisted) {
      actionBtn = `<button class="ack-btn"
          style="margin-left:8px;background:rgba(255,80,80,0.15);border-color:var(--red);color:var(--red);"
          onclick="event.stopPropagation();removeFromWhitelist('${escapeHtml(s.badge)}')"
          title="Remove from whitelist — badge will be monitored for loitering again">
          ✕ Remove WL</button>`;
    } else if (status.cls === 'alert' && s.alert_id) {
      actionBtn = `<button class="ack-btn"
          onclick="event.stopPropagation();ackAlert(${s.alert_id})"
          title="Acknowledge — adds badge to whitelist">
          ✓ Ack</button>`;
    }
    return `
      <tr class="${rowCls} clickable" onclick="showBadgeDetail('${escapeHtml(s.badge)}','${escapeHtml(s.last_camera || '')}')">
        <td class="mono"><b>${escapeHtml(s.badge)}</b></td>
        <td class="mono">${fmtTime(s.first_seen_dt)}</td>
        <td class="mono">${escapeHtml(s.last_camera || '—')} · ${fmtRelative(s.last_seen_dt)}</td>
        <td><span class="duration-pill ${dCls}" data-since="${s.first_seen_dt.getTime()}">
          ${dCls === 'alert' ? '⚠ ' : ''}${fmtDuration(secs)}
        </span></td>
        <td><span class="status-tag ${status.cls}">${status.label}</span>${actionBtn}</td>
        <td class="mono">${s.total_sightings}</td>
      </tr>`;
  }).join('');
}

/* ─── Click handler: open enhanced detail modal (image + history) ─── */
function showBadgeDetail(badge, lastCamera) {
  openBadgeDetail(badge, lastCamera);
}
window.showBadgeDetail = showBadgeDetail;

/* ─── Remove badge from whitelist directly from premises table ─── */
async function removeFromWhitelist(badge) {
  try {
    await removeWhitelist(badge);
    await refreshAll();
  } catch (e) {
    console.error('remove whitelist failed:', e);
  }
}
window.removeFromWhitelist = removeFromWhitelist;

/* ─── Live Notifications panel ─── */
function renderNotifications() {
  const list = document.getElementById('notif-list');
  const meta = document.getElementById('notif-meta');
  if (!list) return;

  if (cachedNotifications.length === 0) {
    list.innerHTML = `<div class="notif-empty">waiting for events…</div>`;
    if (meta) meta.textContent = 'no events yet';
    return;
  }
  if (meta) meta.textContent = `${cachedNotifications.length} event${cachedNotifications.length > 1 ? 's' : ''} today`;

  list.innerHTML = cachedNotifications.map(d => {
    const ts = new Date(d.timestamp.endsWith('Z') ? d.timestamp : d.timestamp + 'Z');
    return `
      <div class="notif-item" onclick='showNotificationImage(${JSON.stringify(d).replace(/'/g, "&#39;")})'>
        <div class="thumb">${thumbSVG(d.badge)}</div>
        <div class="info">
          <div class="title">${escapeHtml(d.badge)} <span class="tag-${d.type}" style="font-size:9px;padding:1px 5px;margin-left:4px;vertical-align:middle;">${d.type.toUpperCase()}</span></div>
          <div class="meta">${escapeHtml(d.camera)} · ${fmtTime(ts)}</div>
        </div>
        <div class="notif-time">${fmtRelative(ts)}</div>
      </div>`;
  }).join('');
}

function showNotificationImage(d) {
  openModal({
    title: d.badge,
    sub: `${d.camera} · ${fmtTime(new Date(d.timestamp))} · ${d.type.toUpperCase()}`,
    filename: d.filename,
    hasImage: d.has_image,
  });
}
window.showNotificationImage = showNotificationImage;

function tickDurations() {
  document.querySelectorAll('[data-since]').forEach(el => {
    const since = parseInt(el.getAttribute('data-since'), 10);
    const secs = (Date.now() - since) / 1000;
    if (el.classList.contains('duration-pill')) {
      const cls = durationClass(secs);
      el.className = `duration-pill ${cls}`;
      el.setAttribute('data-since', since);
      el.innerHTML = `${cls === 'alert' ? '⚠ ' : ''}${fmtDuration(secs)}`;
    } else if (el.classList.contains('duration')) {
      el.textContent = fmtDuration(secs);
    }
  });
}

function connectWS() {
  if (typeof USE_DUMMY_DATA !== 'undefined' && USE_DUMMY_DATA) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try { ws = new WebSocket(`${proto}://${location.host}/ws`); }
  catch (e) { return; }
  ws.onmessage = () => setTimeout(refreshAll, 300);
  ws.onclose = () => setTimeout(connectWS, 2000);
  ws.onerror = () => { try { ws.close(); } catch (e) { } };
}

document.addEventListener('DOMContentLoaded', async () => {
  bootstrap('live');
  await refreshAll();
  setInterval(refreshAll, 5000);
  setInterval(tickDurations, 1000);
  connectWS();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshAll();
  });
});
