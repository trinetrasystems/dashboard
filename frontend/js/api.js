/* ────────────────────────────────────────────────────────────────────────────
 * API LAYER — talks to the FastAPI backend (same origin).
 * ──────────────────────────────────────────────────────────────────────────── */

const API_BASE = '';            // empty = same origin (FastAPI serves the frontend)

/* Signed out or session expired → back to the sign-in page. The page is leaving, so the call never settles: no
   "refresh failed" error from a poll that fired in the instant between sign-out and the sign-in page. */
async function apiCheck(path, r) {
  if (r.status === 401) {
    const page = location.pathname.replace(/^\//, '');
    window.location.replace(page && page !== 'index.html' && page !== 'login.html'
      ? `login.html?next=${encodeURIComponent(page + location.search)}` : 'login.html');
    LEAVING = true;
    return NEVER();
  }
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    throw new Error(data.detail || `${path} ${r.status}`);
  }
  return r.json();
}
/* Signing out: no new request leaves the page any more (a poll sent after the sign-out would only get 401). */
let LEAVING = false;
function leavingPage() { LEAVING = true; }
const NEVER = () => new Promise(() => {});

async function apiGet(path) {
  if (LEAVING) return NEVER();
  const r = await fetch(`${API_BASE}${path}`, { credentials: 'same-origin' });
  return apiCheck(path, r);
}
async function apiPost(path, body) {
  if (LEAVING) return NEVER();
  const r = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(body || {}),
  });
  return apiCheck(path, r);
}
async function apiPut(path, body) {
  if (LEAVING) return NEVER();
  const r = await fetch(`${API_BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(body || {}),
  });
  return apiCheck(path, r);
}
async function apiDelete(path) {
  if (LEAVING) return NEVER();
  const r = await fetch(`${API_BASE}${path}`, { method: 'DELETE', credentials: 'same-origin' });
  return apiCheck(path, r);
}

async function signOutLegacy() { return signOut(); }   // signOut / getMe: shell.js

/* ─── Data fetchers ─── */
async function getStats() {
  return await apiGet('/api/stats');
}

async function getSessions() {
  return await apiGet('/api/sessions/active');
}

async function getDetections(filters = {}) {
  const params = new URLSearchParams();
  if (filters.date)   params.set('date',   filters.date);
  if (filters.month)  params.set('month',  filters.month);
  if (filters.badge)  params.set('badge',  filters.badge);
  if (filters.camera) params.set('camera', filters.camera);
  if (filters.search) params.set('search', filters.search);
  if (filters.zone)   params.set('zone',   filters.zone);
  if (filters.allTime && !filters.date && !filters.month) params.set('all_time', 'true');
  if (filters.limit)  params.set('limit',  filters.limit);
  if (filters.offset) params.set('offset', filters.offset);
  const q = params.toString();
  return await apiGet(`/api/detections${q ? '?' + q : ''}`);
}

function exportDetectionsCSV(filters = {}) {
  // Direct download — opens the export endpoint with current filters
  const params = new URLSearchParams();
  if (filters.date)   params.set('date',   filters.date);
  if (filters.month)  params.set('month',  filters.month);
  if (filters.badge)  params.set('badge',  filters.badge);
  if (filters.camera) params.set('camera', filters.camera);
  if (filters.search) params.set('search', filters.search);
  if (filters.zone)   params.set('zone',   filters.zone);
  if (filters.allTime && !filters.date && !filters.month) params.set('all_time', 'true');
  window.location.href = `/api/detections/export?${params.toString()}`;
}

async function getBadges() {
  return await apiGet('/api/badges');
}

async function getBadgeHistory(badge, limit = 50) {
  return await apiGet(`/api/badges/${encodeURIComponent(badge)}/history?limit=${limit}`);
}

async function getVisits(filters = {}) {
  const params = new URLSearchParams();
  if (filters.badge)  params.set('badge',  filters.badge);
  if (filters.status) params.set('status', filters.status);
  if (filters.date)   params.set('date',   filters.date);
  if (filters.limit)  params.set('limit',  filters.limit);
  const q = params.toString();
  return await apiGet(`/api/visits${q ? '?' + q : ''}`);
}

async function getVisitTimeline(visitId) {
  return await apiGet(`/api/visits/${visitId}/timeline`);
}

async function getBadgeSessions(badge, limit = 20) {
  return await apiGet(`/api/badges/${encodeURIComponent(badge)}/sessions?limit=${limit}`);
}

async function getSessionDetections(sessionId) {
  return await apiGet(`/api/sessions/${sessionId}/detections`);
}

async function getWhitelist() {
  return await apiGet('/api/whitelist');
}

async function addWhitelist(badge, reason) {
  return await apiPost('/api/whitelist', { badge, reason });
}

async function removeWhitelist(badge) {
  return await apiDelete(`/api/whitelist/${encodeURIComponent(badge)}`);
}

/* ─── Perimeter: zone rules + alert log ─── */
/* Aggregates over the WHOLE filter set — the Logs table is paged, so its
   charts must not be computed from the visible 20 rows. */
async function getDetectionsSummary(filters = {}) {
  const params = new URLSearchParams();
  if (filters.date)   params.set('date',   filters.date);
  if (filters.month)  params.set('month',  filters.month);
  if (filters.badge)  params.set('badge',  filters.badge);
  if (filters.camera) params.set('camera', filters.camera);
  if (filters.search) params.set('search', filters.search);
  if (filters.zone)   params.set('zone',   filters.zone);
  if (filters.allTime && !filters.date && !filters.month) params.set('all_time', 'true');
  const q = params.toString();
  return await apiGet(`/api/detections/summary${q ? '?' + q : ''}`);
}

async function getPerimeterRules() {
  return await apiGet('/api/perimeter/rules');
}

async function getRecentAlerts(limit = 50, kind = null, todayOnly = false, status = null) {
  const q = new URLSearchParams({ limit });
  if (kind) q.set('kind', kind);
  if (todayOnly) q.set('today', 'true');
  if (status) q.set('status', status);   // 'open' = every unacknowledged alert, any day
  return await apiGet(`/api/alerts/recent?${q.toString()}`);
}

async function ackAlertAPI(alertId, ackedBy = 'operator') {
  return await apiPost(`/api/alerts/${alertId}/ack`, { acked_by: ackedBy });
}
