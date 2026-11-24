/* ────────────────────────────────────────────────────────────────────────────
 * LIVE PERIMETER PAGE — today only.
 *
 * Zones come from the regions drawn per camera (Alerts → Regions):
 *   1 Perimeter Alert Region — the client's presence rule alerts at once
 *   2 Outside perimeter      — the client's dwell rules give the early warning
 *   (watch regions: their own dwell / presence / count rules)
 *
 * The table lists ONE ROW PER DETECTION; its dwell timer counts against the
 * shortest enabled dwell rule for that class. ALERTS are decided by the
 * backend rule engine (alert_rules.py), never by this page.
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
let _dwellAlertBySession = {};   // session id → its early-warning alert
let _breachAlertByImage = {};    // frame filename → its breach alert
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
  // the Workplace Analytics pager (same markup as shell.js pager()); page is 0-based here
  const el = document.getElementById(elId);
  if (!el) return;
  el.style.display = '';
  el.className = 'pager';
  el.innerHTML = waPagerHtml(page, total, LIVE_PAGE_SIZE, fnName);
}

function gotoDetPage(n) { detPage = n; renderDetectionsTable(); }
function gotoActivePage(n) { activePage = n; renderAlertLog(); }
function gotoAckedPage(n) { ackedPage = n; renderAckedLog(); }
window.gotoDetPage = gotoDetPage;
window.gotoActivePage = gotoActivePage;
window.gotoAckedPage = gotoAckedPage;

/* Timestamps are stored as naive UTC ISO strings */
function utcDate(s) {
  return new Date(String(s).endsWith('Z') ? s : s + 'Z');
}

/* How long the object behind an early warning has been present.
   Counts up until the alert is acknowledged, then holds. */
function presentMinutes(a) {
  if (a.kind === 'breach' || !a.session_first_seen) {
    return Math.floor((a.duration_seconds || 0) / 60);
  }
  const start = utcDate(a.session_first_seen).getTime();
  const end = (a.session_closed && a.session_closed_at)
    ? utcDate(a.session_closed_at).getTime() : Date.now();
  return Math.max(0, Math.floor((end - start) / 60000));
}

/* ─── Zone helpers ─── */
function normBadge(b) {
  return String(b || '').trim().toLowerCase().replace(/_/g, '-').replace('boundary', 'boundry');
}
const COUNTED_RE = /^outside-person-(\d+)-object-(\d+)$/;

function zoneOf(badge) {
  if (!perimeterRules) return 'unknown';
  const k = normBadge(badge);
  if ((perimeterRules.breach_badges || []).includes(k)) return 'breach';
  if ((perimeterRules.outside_badges || []).includes(k)) return 'outside';
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
  if (COUNTED_RE.test(k)) return rules['outside-person'] || null;
  return null;                                   // no enabled dwell rule covers this class
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
  inside:  { cls: 'ok',     label: 'WATCH' },
  unknown: { cls: 'muted',  label: 'UNZONED' },
};

async function refreshAll() {
  try {
    if (!perimeterRules) perimeterRules = await getPerimeterRules();

    const [sessions, stats, detections, openAlerts, todayAlerts] = await Promise.all([
      getSessions(),
      getStats(),
      getDetections({}),                        // defaults to today on the backend
      // EVERY unacknowledged alert, whatever day it fired — an alert must never
      // leave the alert bar without an Ack, including across midnight.
      getRecentAlerts(500, null, false, 'open'),
      getRecentAlerts(200, null, true),         // today's, for the Acknowledged section
    ]);
    const seen = new Set();
    const alerts = [];
    for (const a of [...(openAlerts || []), ...(todayAlerts || [])]) {
      if (!seen.has(a.id)) { seen.add(a.id); alerts.push(a); }
    }
    alerts.sort((x, y) => new Date(y.triggered_at) - new Date(x.triggered_at));

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
    // Every alert is clickable from anywhere (banner, any page of any table),
    // so index them all — not just the rows on the currently visible page.
    _alertMap = {};
    _dwellAlertBySession = {};
    _breachAlertByImage = {};
    for (const a of cachedAlerts) {
      _alertMap[a.id] = a;
      // Breach alerts are one-per-frame; early warnings are one-per-presence.
      if (a.kind === 'breach') {
        if (a.image) _breachAlertByImage[a.image] = a;
      } else if (a.session_id != null && !_dwellAlertBySession[a.session_id]) {
        _dwellAlertBySession[a.session_id] = a;   // newest first → keep the latest
      }
    }

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
const CHIP_MAX_THUMBS = 4;      // per-alert thumbnails shown inside one chip
let _bannerGroups = {};         // group key → group, for the group list modal

function groupAlerts(alerts) {
  const groups = new Map();
  for (const a of alerts) {
    // One slot per class PER CAMERA: the same class on two cameras is two
    // different objects, each with its own Ack.
    // One slot per alert rule PER CAMERA (rules from the Alerts page; older alerts: per class)
    const key = `${a.rule_name || a.badge}|${a.camera}`;
    const crit = a.severity === 'critical' || a.kind === 'breach';
    const g = groups.get(key);
    if (!g) {
      groups.set(key, { key, badge: a.rule_name || a.badge, latest: a, count: 1, ids: [a.id],
                        alerts: [a], breach: crit });
    } else {
      g.count++;
      g.ids.push(a.id);
      g.alerts.push(a);
      g.breach = g.breach || crit;
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

  const breaches = open.filter(a => a.severity === 'critical' || a.kind === 'breach').length;
  const warnings = open.length - breaches;
  const groups = groupAlerts(open);

  banner.style.display = 'block';
  banner.classList.toggle('has-breach', breaches > 0);

  if (head) head.textContent = breaches > 0 ? 'PERIMETER ALERT' : 'WARNING';
  if (sub) {
    const parts = [];
    if (breaches) parts.push(`${breaches} critical`);
    if (warnings) parts.push(`${warnings} warning${warnings === 1 ? '' : 's'}`);
    const extra = groups.length > BANNER_MAX_CHIPS
      ? ` · ${groups.length} rules / cameras` : '';
    sub.textContent = `${parts.join(' · ')}${extra} · unacknowledged`;
  }
  if (ackAll) ackAll.textContent = `✓ ACK ALL (${open.length})`;

  _bannerGroups = {};
  for (const g of groups) _bannerGroups[g.key] = g;

  const shown = groups.slice(0, BANNER_MAX_CHIPS);
  const hiddenGroups = groups.length - shown.length;
  const hiddenAlerts = groups.slice(BANNER_MAX_CHIPS).reduce((n, g) => n + g.count, 0);

  let html = shown.map(g => {
    const a = g.latest;
    const ts = new Date(a.triggered_at.endsWith('Z') ? a.triggered_at : a.triggered_at + 'Z');
    const counts = a.counts || countsOf(a.badge);
    const detail = counts ? countsText(counts) : escapeHtml(a.zone_label);
    const mins = presentMinutes(a);
    const keyAttr = escapeHtml(encodeURIComponent(g.key));

    const repeat = g.count > 1
      ? `<span class="alert-chip-count" title="${g.count} alerts for this class">×${g.count}</span>` : '';

    // One alert: the chip itself opens its snapshot.
    // Several: every alert gets its own clickable thumbnail, and the chip
    // opens the full list — so each one can be viewed, not only the latest.
    let media;
    let chipClick;
    if (g.count === 1) {
      media = a.has_image
        ? `<img class="alert-chip-thumb" src="/images/${encodeURIComponent(a.image)}" alt=""
               onerror="this.outerHTML='<span class=&quot;alert-chip-thumb none&quot;>⊘</span>'">`
        : `<span class="alert-chip-thumb none">⊘</span>`;
      chipClick = `showAlertSnapshot(${a.id})`;
    } else {
      const thumbs = g.alerts.slice(0, CHIP_MAX_THUMBS).map(x => {
        const xt = new Date(x.triggered_at.endsWith('Z') ? x.triggered_at : x.triggered_at + 'Z');
        const inner = x.has_image
          ? `<img src="/images/${encodeURIComponent(x.image)}" alt=""
                 onerror="this.outerHTML='<span>⊘</span>'">`
          : `<span>⊘</span>`;
        return `<button class="mini-thumb" title="${escapeHtml(x.badge)} · ${fmtTime(xt)} — open this alert"
                  onclick="event.stopPropagation();showAlertSnapshot(${x.id})">${inner}</button>`;
      }).join('');
      const extra = g.count > CHIP_MAX_THUMBS
        ? `<button class="mini-thumb more" title="See all ${g.count} alerts"
                   onclick="event.stopPropagation();showAlertGroup('${keyAttr}')">+${g.count - CHIP_MAX_THUMBS}</button>`
        : '';
      media = `<div class="mini-thumbs">${thumbs}${extra}</div>`;
      chipClick = `showAlertGroup('${keyAttr}')`;
    }

    return `
      <div class="alert-chip ${g.breach ? 'breach' : 'warning'} ${g.count > 1 ? 'grouped' : ''}"
           onclick="${chipClick}" title="${g.count > 1 ? `Click to see all ${g.count} alerts` : 'Click to see the snapshot'}">
        <div class="alert-chip-body">
          <div class="alert-chip-kind">${g.breach ? '⚡' : '⏱'} ${escapeHtml(a.rule_name || (g.breach ? 'BREACH' : 'EARLY WARNING'))}${repeat}</div>
          <div class="alert-chip-badge">${escapeHtml(a.message || a.badge)}</div>
          <div class="alert-chip-meta">${escapeHtml(a.camera)} · ${a.rule_id ? escapeHtml(a.zone || '') : detail}${g.breach || a.rule_id ? '' : ` · ${mins} min`} · ${fmtTime(ts)}</div>
          ${g.count > 1 ? media : ''}
        </div>
        ${g.count === 1 ? media : ''}
        <button class="ack-btn-lg" onclick="event.stopPropagation();ackGroup('${encodeURIComponent(JSON.stringify(g.ids))}')"
                title="Acknowledge ${g.count} alert${g.count === 1 ? '' : 's'} for this class">✓ ACK${g.count > 1 ? ` ${g.count}` : ''}</button>
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

/* Every alert in one banner group, each individually openable */
function showAlertGroup(encodedKey) {
  const g = _bannerGroups[decodeURIComponent(encodedKey)];
  if (!g) return;

  ensureModal();
  document.querySelector('.modal')?.classList.add('modal-detail');
  document.getElementById('modal-title').textContent = g.badge;
  document.getElementById('modal-sub').textContent =
    `${g.count} unacknowledged alert${g.count === 1 ? '' : 's'} · click any one to open its snapshot`;

  const rows = g.alerts.map(x => {
    const xt = new Date(x.triggered_at.endsWith('Z') ? x.triggered_at : x.triggered_at + 'Z');
    const isBreach = x.kind === 'breach';
    const mins = presentMinutes(x);
    const c = x.counts || countsOf(x.badge);
    const thumb = x.has_image
      ? `<img src="/images/${encodeURIComponent(x.image)}" alt=""
             onerror="this.outerHTML='<span class=&quot;alert-thumb-none&quot;>⊘</span>'">`
      : `<span class="alert-thumb-none">⊘</span>`;
    return `
      <div class="group-row" onclick="showAlertSnapshot(${x.id}, '${escapeHtml(encodedKey)}')">
        <div class="alert-thumb">${thumb}</div>
        <div class="group-row-body">
          <div class="group-row-top">
            <span class="status-tag ${isBreach ? 'alert' : 'warn'}">${isBreach ? 'BREACH' : 'EARLY WARNING'}</span>
            <b class="mono">${fmtTime(xt)}</b>
          </div>
          <div class="group-row-meta">${escapeHtml(x.camera)} · ${escapeHtml(x.zone_label)}${isBreach ? '' : ` · ${mins} min`}${c ? ` · ${escapeHtml(countsText(c))}` : ''}</div>
        </div>
        <button class="ack-btn-sm" onclick="event.stopPropagation();ackAlert(${x.id});this.closest('.group-row').remove();"
                title="Acknowledge just this alert">✓ ACK</button>
      </div>`;
  }).join('');

  document.getElementById('modal-body').innerHTML = `<div class="group-list">${rows}</div>`;
  document.getElementById('modal-overlay').classList.add('open');
}
window.showAlertGroup = showAlertGroup;

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
      ? `dwell & other rules · ${mins.sort((a, b) => a - b).join('/')} min`
      : 'dwell, count & offline rules';
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
    const parts = Object.entries(rules).filter(([b]) => b.startsWith('outside-')).map(([b, m]) => `${b} ${m} min`);
    rule.textContent = parts.length
      ? `Strip beyond the wall — early warning after ${parts.join(' · ')}`
      : 'Strip beyond the wall — no dwell rule switched on (Alerts → Alert rules)';
  }
  const br = document.getElementById('zone-breach-rule');
  if (br && perimeterRules) {
    const names = perimeterRules.instant_rules || [];
    br.textContent = names.length
      ? `Wall top & concertina wire — alerts at once (${names.join(', ')})`
      : 'Wall top & concertina wire — no instant rule switched on (Alerts → Alert rules)';
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

    // Dwell = CONTINUOUS presence of the session behind this detection.
    // While the object is still being seen the timer ticks; once it has gone
    // quiet for longer than the perimeter gap, dwell freezes at the observed
    // span — it never counts up across days.
    const limit = dwellMinutesFor(d.badge);
    let dwellCell = `<span style="color:var(--text-dim);">—</span>`;
    let status = { cls: 'ok', label: 'LOGGED' };
    let note = '';

    // Once an alert has fired it stays EARLY WARNING / BREACH until someone
    // clicks Ack — it never clears itself because the object walked away.
    const ackedTag = { cls: 'wl', label: '✓ ACKED' };

    if (zone === 'breach') {
      const ba = _breachAlertByImage[d.filename];
      status = (ba && ba.acked) ? ackedTag : { cls: 'alert', label: 'BREACH' };
      dwellCell = `<span class="duration-pill alert">⚡ instant</span>`;
    } else if (limit && d.session_first_seen) {
      const first = utcDate(d.session_first_seen).getTime();
      // Present from first sighting until the alert is acknowledged.
      const open = !d.session_closed;
      const end = open ? Date.now()
                       : (d.session_closed_at ? utcDate(d.session_closed_at).getTime() : Date.now());
      const secs = Math.max(0, (end - first) / 1000);
      const mins = secs / 60;
      const cls = mins >= limit ? 'alert' : (mins >= limit * 0.6 ? 'warn' : 'ok');

      dwellCell = open
        ? `<span class="duration-pill ${cls}" data-since="${first}" data-limit="${limit}">${cls === 'alert' ? '⚠ ' : ''}${fmtDuration(secs)}</span>`
        : `<span class="duration-pill ${cls}">${fmtDuration(secs)}</span>`;

      const wa = _dwellAlertBySession[d.session_id];
      if (wa) {
        status = wa.acked ? ackedTag : { cls: 'alert', label: 'EARLY WARNING' };
      } else if (open && mins >= limit) {
        // Over the limit; the checker raises the alert within 30 seconds.
        status = { cls: 'alert', label: 'EARLY WARNING' };
      } else if (open) {
        status = mins >= limit * 0.6
          ? { cls: 'warn', label: 'APPROACHING' }
          : { cls: 'ok', label: 'WATCHING' };
      } else {
        status = { cls: 'muted', label: 'NO ALERT' };
        note = `seen ${fmtDuration(secs)} · needs ${limit} min`;
      }
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
        <td><span class="status-tag ${status.cls}">${status.label}</span>${note
            ? `<div class="status-note">${escapeHtml(note)}</div>` : ''}</td>
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

  // Acknowledged alerts leave this table and appear under "Acknowledged Alerts".
  const active = cachedAlerts.filter(a => !a.acked && !_ackedLocally.has(a.id));

  if (active.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--text-dim);">no active alerts — everything today has been acknowledged</td></tr>`;
    if (meta) meta.textContent = 'all clear';
    renderLivePager('alertlog-pager', 0, 0, 'gotoActivePage');
    return;
  }

  const breaches = active.filter(a => a.severity === 'critical' || a.kind === 'breach').length;
  if (meta) {
    meta.textContent = `${active.length} active · ${breaches} critical · ${active.length - breaches} warning`;
  }

  activePage = clampPage(activePage, active.length);
  renderLivePager('alertlog-pager', activePage, active.length, 'gotoActivePage');

  tbody.innerHTML = pageSlice(active, activePage).map(a => {
    const ts = new Date(a.triggered_at.endsWith('Z') ? a.triggered_at : a.triggered_at + 'Z');
    const isBreach = a.severity === 'critical' || a.kind === 'breach';
    const mins = presentMinutes(a);
    const counts = a.rule_id ? null : (a.counts || countsOf(a.badge));

    const event = a.rule_id ? (a.message || a.rule_name)
      : isBreach ? 'Object in alert region'
      : `Present ${mins} min · over ${dwellMinutesFor(a.badge) || 10} min limit`;

    const typeTag = isBreach
      ? `<span class="status-tag alert">${escapeHtml(a.rule_name || 'BREACH')}</span>`
      : `<span class="status-tag warn">${escapeHtml(a.rule_name || 'EARLY WARNING')}</span>`;

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

  const breaches = sorted.filter(a => a.severity === 'critical' || a.kind === 'breach').length;
  if (meta) {
    meta.textContent = `${sorted.length} acknowledged · ${breaches} critical · ${sorted.length - breaches} warning`;
  }

  ackedPage = clampPage(ackedPage, sorted.length);
  renderLivePager('ackedlog-pager', ackedPage, sorted.length, 'gotoAckedPage');

  tbody.innerHTML = pageSlice(sorted, ackedPage).map(a => {
    const ts = new Date(a.triggered_at.endsWith('Z') ? a.triggered_at : a.triggered_at + 'Z');
    const isBreach = a.severity === 'critical' || a.kind === 'breach';
    const mins = presentMinutes(a);
    const counts = a.rule_id ? null : (a.counts || countsOf(a.badge));

    const event = a.rule_id ? (a.message || a.rule_name)
      : isBreach ? 'Object in alert region'
      : `Present ${mins} min · over ${dwellMinutesFor(a.badge) || 10} min limit`;

    const typeTag = isBreach
      ? `<span class="status-tag alert">${escapeHtml(a.rule_name || 'BREACH')}</span>`
      : `<span class="status-tag warn">${escapeHtml(a.rule_name || 'EARLY WARNING')}</span>`;

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

function showAlertSnapshot(alertId, fromGroupKey) {
  const a = _alertMap[alertId];
  if (!a) return;
  const ts = new Date(a.triggered_at.endsWith('Z') ? a.triggered_at : a.triggered_at + 'Z');
  const mins = presentMinutes(a);
  const counts = a.counts || countsOf(a.badge);
  const rule = a.kind === 'breach' ? 'instant — no dwell wait' : `dwell ${mins} min`;
  openModal({
    title: a.badge,
    sub: `${a.zone_label} · ${a.camera} · ${fmtTime(ts)} · ${a.kind === 'breach' ? 'BREACH' : 'EARLY WARNING'} · ${rule}` +
         (counts ? ` · ${countsText(counts)}` : ''),
    filename: a.image,
    hasImage: a.has_image,
  });
  // Opened from a group list: offer a way back to the rest of the group
  if (fromGroupKey && _bannerGroups[decodeURIComponent(fromGroupKey)]) {
    const g = _bannerGroups[decodeURIComponent(fromGroupKey)];
    document.getElementById('modal-body').insertAdjacentHTML('afterbegin',
      `<div style="padding: 14px 24px 0;">
         <button class="pager-btn" onclick="showAlertGroup('${escapeHtml(fromGroupKey)}')">← all ${g.count} alerts</button>
       </div>`);
  }
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
      labels += `<text x="${x + barW / 2}" y="${chartH + 18}" text-anchor="middle" font-size="12" fill="var(--text-dim)">${pad(i)}:00</text>`;
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
      <text x="40" y="${chartH + 4}" text-anchor="end" font-size="12" fill="var(--text-dim)">0</text>
      <text x="40" y="${chartH * 0.5 + 4}" text-anchor="end" font-size="12" fill="var(--text-dim)">${Math.floor(max / 2)}</text>
      <text x="40" y="10" text-anchor="end" font-size="12" fill="var(--text-dim)">${max}</text>
    </svg>`;
}

/* ═══ Live-ticking dwell timers ═══ */
function tickDurations() {
  document.querySelectorAll('.duration-pill[data-since]').forEach(el => {
    const since = parseInt(el.getAttribute('data-since'), 10);
    const limit = parseFloat(el.getAttribute('data-limit')) || 15;
    const secs = Math.max(0, (Date.now() - since) / 1000);
    const mins = secs / 60;
    const cls = mins >= limit ? 'alert' : (mins >= limit * 0.6 ? 'warn' : 'ok');
    el.className = `duration-pill ${cls}`;
    el.innerHTML = `${cls === 'alert' ? '⚠ ' : ''}${fmtDuration(secs)}`;
  });
}

function connectWS() {
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
