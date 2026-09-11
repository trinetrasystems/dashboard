/* ────────────────────────────────────────────────────────────────────────────
 * LIVE PERIMETER PAGE — today only.
 *
 * Two zones drive everything (see the Pharma Perimeter deck, slide 4):
 *   1 Perimeter Alert Region — any detection alerts INSTANTLY
 *   2 Outside perimeter      — alerts as an EARLY WARNING after a dwell rule
 *
 * The table lists ONE ROW PER DETECTION. Sessions keep running underneath so
 * the dwell rule still accumulates across frames and fires its early warning.
 *
 * Zone rules come from the backend (/api/perimeter/rules), never hardcoded.
 * ──────────────────────────────────────────────────────────────────────────── */

let activeSessions = [];
let sessionById = {};
let cachedStats = null;
let cachedDetections = [];
let cachedAlerts = [];
let perimeterRules = null;
let _detMap = {};      // detection id → detection, for row-click lookup
let _alertMap = {};    // alert id → alert, for row-click lookup
let _ackedLocally = new Set();   // instant banner dismissal, before refetch

/* Live tables page at 10 rows. The data is already in memory (today only),
   so paging is client-side and instant. */
const LIVE_PAGE_SIZE = 10;
let detPage = 0;
let activePage = 0;
let ackedPage = 0;

/* Clamp a page index to the rows actually available */
function clampPage(page, total) {
  const last = Math.max(0, Math.ceil(total / LIVE_PAGE_SIZE) - 1);
  return Math.min(Math.max(0, page), last);
}

/* Slice the rows for the current page */
function pageSlice(rows, page) {
  const start = page * LIVE_PAGE_SIZE;
  return rows.slice(start, start + LIVE_PAGE_SIZE);
}

/* Shared pager UI — hidden when everything fits on one page */
function renderLivePager(elId, page, total, fnName) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (total <= LIVE_PAGE_SIZE) { el.style.display = 'none'; return; }

  const last = Math.max(0, Math.ceil(total / LIVE_PAGE_SIZE) - 1);
  const from = page * LIVE_PAGE_SIZE + 1;
  const to = Math.min(total, (page + 1) * LIVE_PAGE_SIZE);
  const atStart = page === 0;
  const atEnd = page >= last;

  el.style.display = 'flex';
  el.innerHTML = `
    <button class="pager-btn" ${atStart ? 'disabled' : ''} onclick="${fnName}(0)">« First</button>
    <button class="pager-btn" ${atStart ? 'disabled' : ''} onclick="${fnName}(${page - 1})">‹ Prev</button>
    <span class="pager-status">${from}–${to} of ${total} · page ${page + 1} of ${last + 1}</span>
    <button class="pager-btn" ${atEnd ? 'disabled' : ''} onclick="${fnName}(${page + 1})">Next ›</button>
    <button class="pager-btn" ${atEnd ? 'disabled' : ''} onclick="${fnName}(${last})">Last »</button>`;
}

function gotoDetPage(n) { detPage = n; renderDetectionsTable(); }
function gotoActivePage(n) { activePage = n; renderAlertLog(); }
function gotoAckedPage(n) { ackedPage = n; renderAckedLog(); }
window.gotoDetPage = gotoDetPage;
window.gotoActivePage = gotoActivePage;
window.gotoAckedPage = gotoAckedPage;

/* ─── Zone helpers ─── */
function normBadge(b) {
  return String(b || '').trim().toLowerCase().replace(/_/g, '-').replace('boundary', 'boundry');
}
const COUNTED_RE = /^outside-person-(\d+)-object-(\d+)$/;

function zoneOf(badge) {
  if (!perimeterRules) return 'unknown';
  const k = normBadge(badge);
  if ((perimeterRules.breach_badges || []).includes(k)) return 'breach';
  if (Object.prototype.hasOwnProperty.call(perimeterRules.dwell_rules || {}, k)) return 'outside';
  if (COUNTED_RE.test(k)) return 'outside';
  if ((perimeterRules.inside_badges || []).includes(k)) return 'inside';
  return 'unknown';
}
function zoneLabel(zone) {
  const labels = (perimeterRules && perimeterRules.zone_labels) || {};
  return labels[zone] || zone;
}
function dwellMinutesFor(badge) {
  if (!perimeterRules) return null;
  const k = normBadge(badge);
  if ((perimeterRules.breach_badges || []).includes(k)) return null;
  const rules = perimeterRules.dwell_rules || {};
  if (Object.prototype.hasOwnProperty.call(rules, k)) return rules[k];
  if (COUNTED_RE.test(k)) return rules['outside-person'] || 10;
  if ((perimeterRules.inside_badges || []).includes(k)) return null;
  return perimeterRules.default_dwell_minutes;
}
/* "Outside-person-3-object-2" → {persons: 3, objects: 2} */
function countsOf(badge) {
  const m = COUNTED_RE.exec(normBadge(badge));
  return m ? { persons: +m[1], objects: +m[2] } : null;
}
function countsText(c) {
  if (!c) return '';
  const p = `${c.persons} person${c.persons === 1 ? '' : 's'}`;
  const o = `${c.objects} object${c.objects === 1 ? '' : 's'}`;
  return `${p} · ${o}`;
}

const ZONE_TAG = {
  breach:  { cls: 'breach', label: 'ALERT REGION' },
  outside: { cls: 'warn',   label: 'OUTSIDE' },
  inside:  { cls: 'ok',     label: 'INSIDE' },
  unknown: { cls: 'muted',  label: 'UNZONED' },
};

async function refreshAll() {
  try {
    if (!perimeterRules) perimeterRules = await getPerimeterRules();

    const [sessions, stats, detections, alerts] = await Promise.all([
      getSessions(),
      getStats(),
      getDetections({}),            // defaults to today on the backend
      getRecentAlerts(60, null, true),   // today only
    ]);

    activeSessions = sessions || [];
    sessionById = {};
    for (const s of activeSessions) {
      sessionById[s.id] = {
        ...s,
        first_seen_dt: new Date(s.first_seen.endsWith('Z') ? s.first_seen : s.first_seen + 'Z'),
      };
    }
    cachedStats = stats;
    cachedDetections = detections || [];
    cachedAlerts = alerts || [];

    updateTopbarPills(cachedStats);
    renderBanner();
    renderKPIs();
    renderZoneStrip();
    renderDetectionsTable();
    renderAlertLog();
    renderAckedLog();
    renderTodayChart();
  } catch (e) {
    console.error('refresh failed:', e);
  }
}

/* ═══ Flashing alert banner ═══
 * Grouped by badge so a class alerting repeatedly takes ONE slot, capped so
 * hundreds of alerts can never flood the top of the page.
 * ═══════════════════════════════════════════════════════════════════════ */

const BANNER_MAX_CHIPS = 6;

function groupAlerts(alerts) {
  const groups = new Map();
  for (const a of alerts) {
    const key = a.badge;                 // one slot per badge
    const g = groups.get(key);
    if (!g) {
      groups.set(key, { badge: a.badge, latest: a, count: 1, ids: [a.id], breach: a.kind === 'breach' });
    } else {
      g.count++;
      g.ids.push(a.id);
      g.breach = g.breach || a.kind === 'breach';
      // alerts arrive newest-first, so the first one seen is the latest
    }
  }
  // Breaches first, then the most recent
  return [...groups.values()].sort((x, y) => {
    if (x.breach !== y.breach) return x.breach ? -1 : 1;
    return new Date(y.latest.triggered_at) - new Date(x.latest.triggered_at);
  });
}

function renderBanner() {
  const banner = document.getElementById('breach-banner');
  const list = document.getElementById('breach-list');
  const head = document.getElementById('banner-head');
  const sub = document.getElementById('banner-sub');
  const ackAll = document.getElementById('ack-all-btn');
  if (!banner || !list) return;

  const open = cachedAlerts.filter(a => !a.acked && !_ackedLocally.has(a.id));
  if (open.length === 0) {
    banner.style.display = 'none';
    banner.classList.remove('has-breach');
    return;
  }

  const breaches = open.filter(a => a.kind === 'breach').length;
  const warnings = open.length - breaches;
  const groups = groupAlerts(open);

  banner.style.display = 'block';
  banner.classList.toggle('has-breach', breaches > 0);

  if (head) head.textContent = breaches > 0 ? 'PERIMETER BREACH' : 'EARLY WARNING';
  if (sub) {
    const parts = [];
    if (breaches) parts.push(`${breaches} breach`);
    if (warnings) parts.push(`${warnings} early warning`);
    const extra = groups.length > BANNER_MAX_CHIPS
      ? ` · ${groups.length} classes` : '';
    sub.textContent = `${parts.join(' · ')}${extra} · unacknowledged`;
  }
  if (ackAll) ackAll.textContent = `✓ ACK ALL (${open.length})`;

  const shown = groups.slice(0, BANNER_MAX_CHIPS);
  const hiddenGroups = groups.length - shown.length;
  const hiddenAlerts = groups.slice(BANNER_MAX_CHIPS).reduce((n, g) => n + g.count, 0);

  let html = shown.map(g => {
    const a = g.latest;
    const ts = new Date(a.triggered_at.endsWith('Z') ? a.triggered_at : a.triggered_at + 'Z');
    const counts = a.counts || countsOf(a.badge);
    const detail = counts ? countsText(counts) : escapeHtml(a.zone_label);
    const mins = Math.floor((a.duration_seconds || 0) / 60);

    const thumb = a.has_image
      ? `<img class="alert-chip-thumb" src="/images/${encodeURIComponent(a.image)}" alt=""
             onerror="this.outerHTML='<span class=&quot;alert-chip-thumb none&quot;>⊘</span>'">`
      : `<span class="alert-chip-thumb none">⊘</span>`;

    const repeat = g.count > 1
      ? `<span class="alert-chip-count" title="${g.count} alerts for this class">×${g.count}</span>` : '';

    return `
      <div class="alert-chip ${g.breach ? 'breach' : 'warning'}"
           onclick="showAlertSnapshot(${a.id})" title="Click to see the snapshot">
        ${thumb}
        <div class="alert-chip-body">
          <div class="alert-chip-kind">${g.breach ? '⚡ BREACH' : '⏱ EARLY WARNING'}${repeat}</div>
          <div class="alert-chip-badge">${escapeHtml(a.badge)}</div>
          <div class="alert-chip-meta">${escapeHtml(a.camera)} · ${detail}${g.breach ? '' : ` · ${mins} min`} · ${fmtTime(ts)}</div>
        </div>
        <button class="ack-btn-lg" onclick="event.stopPropagation();ackGroup('${encodeURIComponent(JSON.stringify(g.ids))}')"
                title="Acknowledge ${g.count} alert${g.count === 1 ? '' : 's'} for this class">✓ ACK</button>
      </div>`;
  }).join('');

  if (hiddenGroups > 0) {
    html += `
      <div class="alert-chip more-chip" title="Acknowledge the visible ones to reveal these">
        <div class="alert-chip-body">
          <div class="alert-chip-badge">+${hiddenGroups} more class${hiddenGroups === 1 ? '' : 'es'}</div>
          <div class="alert-chip-meta">${hiddenAlerts} further alert${hiddenAlerts === 1 ? '' : 's'} · see the log below</div>
        </div>
      </div>`;
  }

  list.innerHTML = html;
}

/* Ack every alert in one badge group */
async function ackGroup(idsJson) {
  let ids;
  try { ids = JSON.parse(decodeURIComponent(idsJson)); }
  catch (e) { return; }
  ids.forEach(id => _ackedLocally.add(id));
  renderBanner();
  try {
    await Promise.all(ids.map(id => ackAlertAPI(id)));
    await refreshAll();
  } catch (e) {
    console.error('ack group failed:', e);
    ids.forEach(id => _ackedLocally.delete(id));
    renderBanner();
  }
}
window.ackGroup = ackGroup;

/* Ack everything currently unacknowledged — the escape hatch for alert storms */
async function ackAllAlerts() {
  const open = cachedAlerts.filter(a => !a.acked && !_ackedLocally.has(a.id));
  const ids = open.map(a => a.id);
  if (ids.length === 0) return;
  ids.forEach(id => _ackedLocally.add(id));
  renderBanner();
  try {
    // Send in small batches so a storm does not open hundreds of sockets at once
    for (let i = 0; i < ids.length; i += 10) {
      await Promise.all(ids.slice(i, i + 10).map(id => ackAlertAPI(id)));
    }
    await refreshAll();
  } catch (e) {
    console.error('ack all failed:', e);
    await refreshAll();
  }
}
window.ackAllAlerts = ackAllAlerts;

/* Ack a single alert (used by the alert log) */
async function ackAlert(alertId) {
  _ackedLocally.add(alertId);
  renderBanner();
  try {
    await ackAlertAPI(alertId);
    await refreshAll();
  } catch (e) {
    console.error('ack failed:', e);
    _ackedLocally.delete(alertId);
    renderBanner();
  }
}
window.ackAlert = ackAlert;

/* ═══ KPIs ═══ */
function renderKPIs() {
  if (!cachedStats) return;
  document.getElementById('kpi-breach').textContent = cachedStats.breaches_today || 0;
  document.getElementById('kpi-warning').textContent = cachedStats.warnings_today || 0;
  document.getElementById('kpi-total').textContent =
    cachedStats.alerts_today != null
      ? cachedStats.alerts_today
      : (cachedStats.breaches_today || 0) + (cachedStats.warnings_today || 0);

  const sub = document.getElementById('kpi-warning-sub');
  if (sub && perimeterRules) {
    const mins = [...new Set(Object.values(perimeterRules.dwell_rules || {}))];
    sub.textContent = mins.length
      ? `dwell · outside perimeter · ${mins.sort((a, b) => a - b).join('/')} min`
      : 'dwell · outside perimeter';
  }
}

/* ═══ Zone strip ═══ */
function renderZoneStrip() {
  if (!cachedStats) return;
  const z = cachedStats.zone_detections_today || {};
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.textContent = v || 0;
  };
  set('zone-breach-count', z.breach);
  set('zone-outside-count', z.outside);

  const rule = document.getElementById('zone-outside-rule');
  if (rule && perimeterRules) {
    const rules = perimeterRules.dwell_rules || {};
    const parts = Object.entries(rules).map(([b, m]) => `${b} ${m} min`);
    rule.textContent = parts.length
      ? `Strip beyond the wall — early warning after ${parts.join(' · ')}`
      : 'Strip beyond the wall — dwell-based early warning';
  }
}

/* ═══ Today's detections — ONE ROW PER DETECTION ═══ */
function renderDetectionsTable() {
  _detMap = {};
  const tbody = document.getElementById('watch-body');
  const sub = document.getElementById('watch-sub');
  if (!tbody) return;

  if (cachedDetections.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:24px;color:var(--text-dim);">no detections today</td></tr>`;
    if (sub) sub.textContent = 'one row per detection · today only';
    renderLivePager('watch-pager', 0, 0, 'gotoDetPage');
    return;
  }
  if (sub) {
    sub.textContent = `${cachedDetections.length} detection${cachedDetections.length === 1 ? '' : 's'} today · one row each`;
  }

  detPage = clampPage(detPage, cachedDetections.length);
  renderLivePager('watch-pager', detPage, cachedDetections.length, 'gotoDetPage');

  tbody.innerHTML = pageSlice(cachedDetections, detPage).map(d => {
    _detMap[d.id] = d;
    const ts = new Date(d.timestamp.endsWith('Z') ? d.timestamp : d.timestamp + 'Z');
    const zone = d.zone || zoneOf(d.badge);
    const zt = ZONE_TAG[zone] || ZONE_TAG.unknown;
    const counts = d.counts || countsOf(d.badge);

    // Dwell comes from the still-open session behind this detection, so the
    // timer keeps accumulating across frames even though rows are per-detection.
    const sess = d.session_id ? sessionById[d.session_id] : null;
    const limit = dwellMinutesFor(d.badge);
    let dwellCell = `<span style="color:var(--text-dim);">—</span>`;
    let status = { cls: 'ok', label: 'LOGGED' };

    if (zone === 'breach') {
      status = { cls: 'alert', label: 'BREACH' };
      dwellCell = `<span class="duration-pill alert">⚡ instant</span>`;
    } else if (sess && limit) {
      const secs = (Date.now() - sess.first_seen_dt.getTime()) / 1000;
      const mins = secs / 60;
      const cls = mins >= limit ? 'alert' : (mins >= limit * 0.6 ? 'warn' : 'ok');
      dwellCell = `<span class="duration-pill ${cls}" data-since="${sess.first_seen_dt.getTime()}" data-limit="${limit}">
          ${cls === 'alert' ? '⚠ ' : ''}${fmtDuration(secs)}</span>`;
      status = mins >= limit
        ? { cls: 'alert', label: 'EARLY WARNING' }
        : (mins >= limit * 0.6 ? { cls: 'warn', label: 'APPROACHING' }
                               : { cls: 'ok', label: 'WATCHING' });
    }

    const countBadge = counts
      ? `<span class="count-chip" title="${escapeHtml(countsText(counts))}">👤 ${counts.persons} · 📦 ${counts.objects}</span>`
      : '';

    return `
      <tr class="clickable ${zone === 'breach' ? 'alert-row' : ''}" onclick="showDetectionDetail(${d.id})">
        <td class="mono"><b>${escapeHtml(d.badge)}</b>
            <span class="zone-tag ${zt.cls}" style="margin-left:6px;">${zt.label}</span>${countBadge}</td>
        <td class="mono">${escapeHtml(d.camera)}</td>
        <td class="mono">${fmtTime(ts)}</td>
        <td>${dwellCell}</td>
        <td><span class="status-tag ${status.cls}">${status.label}</span></td>
      </tr>`;
  }).join('');
}

/* Row click — snapshot plus the person/object tally for counted classes */
function showDetectionDetail(id) {
  const d = _detMap[id];
  if (!d) return;
  const ts = new Date(d.timestamp.endsWith('Z') ? d.timestamp : d.timestamp + 'Z');
  const zone = d.zone || zoneOf(d.badge);
  const counts = d.counts || countsOf(d.badge);

  ensureModal();
  document.querySelector('.modal')?.classList.remove('modal-detail');
  document.getElementById('modal-title').textContent = d.badge;
  document.getElementById('modal-sub').textContent =
    `${d.zone_label || zoneLabel(zone)} · ${d.camera} · ${fmtTime(ts)} · ${fmtDate(ts)}`;

  const tally = counts ? `
    <div class="count-panel">
      <div class="count-panel-head">Currently detected in this frame</div>
      <div class="count-panel-grid">
        <div class="count-box">
          <div class="count-box-value">${counts.persons}</div>
          <div class="count-box-label">person${counts.persons === 1 ? '' : 's'}</div>
        </div>
        <div class="count-box">
          <div class="count-box-value">${counts.objects}</div>
          <div class="count-box-label">object${counts.objects === 1 ? '' : 's'}</div>
        </div>
      </div>
      <div class="count-panel-foot">${escapeHtml(countsText(counts))} outside the perimeter wall</div>
    </div>` : '';

  const img = d.has_image
    ? `<img class="modal-img" src="/images/${encodeURIComponent(d.filename)}" alt="${escapeHtml(d.badge)}"
           onerror="this.outerHTML='<div class=&quot;modal-no-image&quot;>⊘ NO IMAGE AVAILABLE</div>'">`
    : `<div class="modal-no-image">⊘ NO IMAGE AVAILABLE</div>`;

  document.getElementById('modal-body').innerHTML = tally + img;
  document.getElementById('modal-overlay').classList.add('open');
}
window.showDetectionDetail = showDetectionDetail;

/* ═══ Alert log — today ═══ */
function renderAlertLog() {
  const tbody = document.getElementById('alertlog-body');
  const meta = document.getElementById('alertlog-meta');
  if (!tbody) return;
  _alertMap = {};

  // Acknowledged alerts leave this table and appear under "Acknowledged Alerts".
  const active = cachedAlerts.filter(a => !a.acked && !_ackedLocally.has(a.id));

  if (active.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--text-dim);">no active alerts — everything today has been acknowledged</td></tr>`;
    if (meta) meta.textContent = 'all clear';
    renderLivePager('alertlog-pager', 0, 0, 'gotoActivePage');
    return;
  }

  const breaches = active.filter(a => a.kind === 'breach').length;
  if (meta) {
    meta.textContent = `${active.length} active · ${breaches} breach · ${active.length - breaches} early warning`;
  }

  activePage = clampPage(activePage, active.length);
  renderLivePager('alertlog-pager', activePage, active.length, 'gotoActivePage');

  tbody.innerHTML = pageSlice(active, activePage).map(a => {
    _alertMap[a.id] = a;
    const ts = new Date(a.triggered_at.endsWith('Z') ? a.triggered_at : a.triggered_at + 'Z');
    const isBreach = a.kind === 'breach';
    const mins = Math.floor((a.duration_seconds || 0) / 60);
    const counts = a.counts || countsOf(a.badge);

    const event = isBreach
      ? 'Object in alert region'
      : (mins ? `Dwell exceeded · ${mins} min` : 'Dwell exceeded');

    const typeTag = isBreach
      ? `<span class="status-tag alert">BREACH</span>`
      : `<span class="status-tag warn">EARLY WARNING</span>`;

    const thumb = a.has_image
      ? `<div class="alert-thumb"><img src="/images/${encodeURIComponent(a.image)}" alt="" loading="lazy"
             onerror="this.parentNode.innerHTML='<span class=&quot;alert-thumb-none&quot;>⊘</span>'"></div>`
      : `<div class="alert-thumb"><span class="alert-thumb-none">⊘</span></div>`;

    return `
      <tr class="clickable ${isBreach ? 'alert-row' : ''}" onclick="showAlertSnapshot(${a.id})">
        <td>${thumb}</td>
        <td class="mono">${fmtTime(ts)}</td>
        <td>${escapeHtml(event)}
            <button class="ack-btn-sm" onclick="event.stopPropagation();ackAlert(${a.id})"
                    title="Acknowledge — moves this alert to the Acknowledged section">✓ ACK</button></td>
        <td class="mono" style="color:var(--text-muted);">${escapeHtml(a.zone_label)}<br>
            <span style="color:var(--text-dim);font-size:0.85em;">${escapeHtml(a.camera)}</span></td>
        <td class="mono"><b>${escapeHtml(a.badge)}</b>${counts ? `<br><span style="color:var(--text-dim);font-size:0.85em;">${escapeHtml(countsText(counts))}</span>` : ''}</td>
        <td>${typeTag}</td>
      </tr>`;
  }).join('');
}

/* ═══ Acknowledged alerts — what an operator has already actioned ═══
 * Acking does not delete anything: the alert moves here so it can be
 * reviewed later, with who acknowledged it and when.
 * ═══════════════════════════════════════════════════════════════════ */
function renderAckedLog() {
  const tbody = document.getElementById('ackedlog-body');
  const meta = document.getElementById('ackedlog-meta');
  if (!tbody) return;

  const acked = cachedAlerts.filter(a => a.acked || _ackedLocally.has(a.id));

  if (acked.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--text-dim);">nothing acknowledged yet today</td></tr>`;
    if (meta) meta.textContent = 'none yet';
    renderLivePager('ackedlog-pager', 0, 0, 'gotoAckedPage');
    return;
  }

  // Most recently acknowledged first
  const sorted = [...acked].sort((x, y) =>
    new Date(y.acked_at || y.triggered_at) - new Date(x.acked_at || x.triggered_at));

  const breaches = sorted.filter(a => a.kind === 'breach').length;
  if (meta) {
    meta.textContent = `${sorted.length} acknowledged · ${breaches} breach · ${sorted.length - breaches} early warning`;
  }

  ackedPage = clampPage(ackedPage, sorted.length);
  renderLivePager('ackedlog-pager', ackedPage, sorted.length, 'gotoAckedPage');

  tbody.innerHTML = pageSlice(sorted, ackedPage).map(a => {
    _alertMap[a.id] = a;
    const ts = new Date(a.triggered_at.endsWith('Z') ? a.triggered_at : a.triggered_at + 'Z');
    const isBreach = a.kind === 'breach';
    const mins = Math.floor((a.duration_seconds || 0) / 60);
    const counts = a.counts || countsOf(a.badge);

    const event = isBreach
      ? 'Object in alert region'
      : (mins ? `Dwell exceeded · ${mins} min` : 'Dwell exceeded');

    const typeTag = isBreach
      ? `<span class="status-tag alert">BREACH</span>`
      : `<span class="status-tag warn">EARLY WARNING</span>`;

    const thumb = a.has_image
      ? `<div class="alert-thumb"><img src="/images/${encodeURIComponent(a.image)}" alt="" loading="lazy"
             onerror="this.parentNode.innerHTML='<span class=&quot;alert-thumb-none&quot;>⊘</span>'"></div>`
      : `<div class="alert-thumb"><span class="alert-thumb-none">⊘</span></div>`;

    let ackCell;
    if (a.acked_at) {
      const at = new Date(a.acked_at.endsWith('Z') ? a.acked_at : a.acked_at + 'Z');
      ackCell = `<span class="status-tag wl">✓ ACKED</span>
                 <div class="acked-meta">${escapeHtml(a.acked_by || 'operator')} · ${fmtTime(at)}</div>`;
    } else {
      ackCell = `<span class="status-tag wl">✓ ACKED</span>
                 <div class="acked-meta">just now</div>`;
    }

    return `
      <tr class="clickable acked-row" onclick="showAlertSnapshot(${a.id})">
        <td>${thumb}</td>
        <td class="mono">${fmtTime(ts)}</td>
        <td>${escapeHtml(event)} ${typeTag}</td>
        <td class="mono" style="color:var(--text-muted);">${escapeHtml(a.zone_label)}<br>
            <span style="color:var(--text-dim);font-size:0.85em;">${escapeHtml(a.camera)}</span></td>
        <td class="mono"><b>${escapeHtml(a.badge)}</b>${counts ? `<br><span style="color:var(--text-dim);font-size:0.85em;">${escapeHtml(countsText(counts))}</span>` : ''}</td>
        <td>${ackCell}</td>
      </tr>`;
  }).join('');
}

function showAlertSnapshot(alertId) {
  const a = _alertMap[alertId];
  if (!a) return;
  const ts = new Date(a.triggered_at.endsWith('Z') ? a.triggered_at : a.triggered_at + 'Z');
  const mins = Math.floor((a.duration_seconds || 0) / 60);
  const counts = a.counts || countsOf(a.badge);
  const rule = a.kind === 'breach' ? 'instant — no dwell wait' : `dwell ${mins} min`;
  openModal({
    title: a.badge,
    sub: `${a.zone_label} · ${a.camera} · ${fmtTime(ts)} · ${a.kind === 'breach' ? 'BREACH' : 'EARLY WARNING'} · ${rule}` +
         (counts ? ` · ${countsText(counts)}` : ''),
    filename: a.image,
    hasImage: a.has_image,
  });
}
window.showAlertSnapshot = showAlertSnapshot;

/* ═══ Activity chart ═══ */
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
    bars += `<rect x="${x}" y="${chartH - h}" width="${barW}" height="${h}" rx="3" fill="var(--cyan)" opacity="0.85"/>`;
    if (i % 4 === 0) {
      labels += `<text x="${x + barW / 2}" y="${chartH + 18}" text-anchor="middle" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">${pad(i)}:00</text>`;
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
      <text x="40" y="${chartH + 4}" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">0</text>
      <text x="40" y="${chartH * 0.5 + 4}" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">${Math.floor(max / 2)}</text>
      <text x="40" y="10" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">${max}</text>
    </svg>`;
}

/* ═══ Live-ticking dwell timers ═══ */
function tickDurations() {
  document.querySelectorAll('.duration-pill[data-since]').forEach(el => {
    const since = parseInt(el.getAttribute('data-since'), 10);
    const limit = parseFloat(el.getAttribute('data-limit')) || 15;
    const secs = (Date.now() - since) / 1000;
    const mins = secs / 60;
    const cls = mins >= limit ? 'alert' : (mins >= limit * 0.6 ? 'warn' : 'ok');
    el.className = `duration-pill ${cls}`;
    el.innerHTML = `${cls === 'alert' ? '⚠ ' : ''}${fmtDuration(secs)}`;
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
