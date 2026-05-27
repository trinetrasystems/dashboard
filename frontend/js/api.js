/* ────────────────────────────────────────────────────────────────────────────
 * API LAYER — talks to the FastAPI backend.
 *
 * Replaces dummy-data.js when wiring to the real backend.
 * Same data shapes — pages don't need to know it's async.
 *
 * Set USE_DUMMY_DATA = true to fall back to in-memory dummy data (no backend).
 * ──────────────────────────────────────────────────────────────────────────── */

const USE_DUMMY_DATA = false;   // ← set true to bypass backend for offline preview
const API_BASE = '';            // empty = same origin (FastAPI serves the frontend)

async function apiGet(path) {
  const r = await fetch(`${API_BASE}${path}`);
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}
async function apiDelete(path) {
  const r = await fetch(`${API_BASE}${path}`, { method: 'DELETE' });
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}

/* ─── Data fetchers ─── */
async function getStats() {
  if (USE_DUMMY_DATA) return dummyStats();
  return await apiGet('/api/stats');
}

async function getSessions() {
  if (USE_DUMMY_DATA) {
    // Convert dummy data to API shape
    return DUMMY_SESSIONS.map(s => ({
      id: s.id, badge: s.badge,
      first_seen: s.first_seen.toISOString(),
      last_seen: s.last_seen.toISOString(),
      last_camera: s.last_camera,
      total_sightings: s.total_sightings,
      whitelisted: s.whitelisted || false,
      acked: s.acked || false,
      acked_by: s.acked_by || null,
      acked_at: s.acked_at ? s.acked_at.toISOString() : null,
      alert_id: null,  // dummy
    }));
  }
  return await apiGet('/api/sessions/active');
}

async function getDetections(filters = {}) {
  if (USE_DUMMY_DATA) {
    return DUMMY_DETECTIONS.map(d => ({
      id: d.id, filename: d.filename, camera: d.camera, badge: d.badge,
      timestamp: d.timestamp.toISOString(), type: d.type, has_image: d.has_image,
    }));
  }
  const params = new URLSearchParams();
  if (filters.date)   params.set('date',   filters.date);
  if (filters.month)  params.set('month',  filters.month);
  if (filters.badge)  params.set('badge',  filters.badge);
  if (filters.camera) params.set('camera', filters.camera);
  if (filters.search) params.set('search', filters.search);
  const q = params.toString();
  return await apiGet(`/api/detections${q ? '?' + q : ''}`);
}

function exportDetectionsCSV(filters = {}) {
  // Direct download — opens the export endpoint with current filters
  if (USE_DUMMY_DATA) {
    return csvFromDummy();
  }
  const params = new URLSearchParams();
  if (filters.date)   params.set('date',   filters.date);
  if (filters.month)  params.set('month',  filters.month);
  if (filters.badge)  params.set('badge',  filters.badge);
  if (filters.camera) params.set('camera', filters.camera);
  if (filters.search) params.set('search', filters.search);
  window.location.href = `/api/detections/export?${params.toString()}`;
}

async function getBadges() {
  if (USE_DUMMY_DATA) {
    // Hardcoded fallback for offline preview
    return {
      badges: [
        'RED-CIRCLE','BLUE-SQUARE','GREEN-TRIANGLE','YELLOW-STAR',
        'CYAN-PENTAGON','AMBER-TRIANGLE','VIOLET-HEXAGON','ORANGE-DIAMOND',
        'PINK-OCTAGON','WHITE-CROSS','BLACK-SHIELD','GOLD-STAR','SILVER-DIAMOND',
      ].map(b => ({ badge: b, in_classes_file: true, auto_discovered: false, whitelisted: false })),
    };
  }
  return await apiGet('/api/badges');
}

async function getBadgeHistory(badge, limit = 50) {
  if (USE_DUMMY_DATA) {
    return DUMMY_DETECTIONS
      .filter(d => d.badge === badge)
      .slice(0, limit)
      .map(d => ({
        id: d.id, filename: d.filename, camera: d.camera, badge: d.badge,
        timestamp: d.timestamp.toISOString(), type: d.type, has_image: d.has_image,
        session_id: d.session_id || null,
      }));
  }
  return await apiGet(`/api/badges/${encodeURIComponent(badge)}/history?limit=${limit}`);
}

async function getVisits(filters = {}) {
  if (USE_DUMMY_DATA) {
    // Build dummy visits from sessions
    return DUMMY_SESSIONS.map(s => ({
      id: s.id,
      badge: s.badge,
      first_seen: s.first_seen.toISOString(),
      last_seen: s.last_seen.toISOString(),
      duration_seconds: Math.floor((s.last_seen - s.first_seen) / 1000),
      total_sightings: s.total_sightings,
      last_camera: s.last_camera,
      closed: false,
      closed_at: null,
      cameras: { [s.last_camera]: s.total_sightings },
      whitelisted: s.whitelisted || false,
    }));
  }
  const params = new URLSearchParams();
  if (filters.badge)  params.set('badge',  filters.badge);
  if (filters.status) params.set('status', filters.status);
  if (filters.date)   params.set('date',   filters.date);
  if (filters.limit)  params.set('limit',  filters.limit);
  const q = params.toString();
  return await apiGet(`/api/visits${q ? '?' + q : ''}`);
}

async function getVisitTimeline(visitId) {
  if (USE_DUMMY_DATA) {
    const sess = DUMMY_SESSIONS.find(s => s.id === visitId);
    if (!sess) return { visit: null, detections: [] };
    return {
      visit: {
        id: sess.id, badge: sess.badge,
        first_seen: sess.first_seen.toISOString(),
        last_seen: sess.last_seen.toISOString(),
        total_sightings: sess.total_sightings,
        closed: false, closed_at: null,
        last_camera: sess.last_camera,
      },
      detections: DUMMY_DETECTIONS
        .filter(d => d.badge === sess.badge)
        .slice(0, 15)
        .reverse()
        .map(d => ({
          id: d.id, filename: d.filename, camera: d.camera, badge: d.badge,
          timestamp: d.timestamp.toISOString(), type: d.type, has_image: d.has_image,
        })),
    };
  }
  return await apiGet(`/api/visits/${visitId}/timeline`);
}

async function getBadgeSessions(badge, limit = 20) {
  if (USE_DUMMY_DATA) {
    // Fake 3 sessions for demo
    const now = Date.now();
    return [
      { id: 3, badge, first_seen: new Date(now - 30*60000).toISOString(),
        last_seen: new Date(now).toISOString(), last_camera: 'cam-entrance',
        total_sightings: 12, closed: false, closed_at: null,
        duration_seconds: 1800, had_alert: false },
      { id: 2, badge, first_seen: new Date(now - 4*3600000).toISOString(),
        last_seen: new Date(now - 2*3600000).toISOString(), last_camera: 'cam-gate',
        total_sightings: 18, closed: true, closed_at: new Date(now - 2*3600000).toISOString(),
        duration_seconds: 2*3600, had_alert: false },
      { id: 1, badge, first_seen: new Date(now - 8*3600000).toISOString(),
        last_seen: new Date(now - 6*3600000).toISOString(), last_camera: 'cam-gate',
        total_sightings: 25, closed: true, closed_at: new Date(now - 6*3600000).toISOString(),
        duration_seconds: 2*3600, had_alert: true },
    ];
  }
  return await apiGet(`/api/badges/${encodeURIComponent(badge)}/sessions?limit=${limit}`);
}

async function getSessionDetections(sessionId) {
  if (USE_DUMMY_DATA) {
    return DUMMY_DETECTIONS.slice(0, 8).map(d => ({
      id: d.id, filename: d.filename, camera: d.camera, badge: d.badge,
      timestamp: d.timestamp.toISOString(), type: d.type, has_image: d.has_image,
    }));
  }
  return await apiGet(`/api/sessions/${sessionId}/detections`);
}

async function getWhitelist() {
  if (USE_DUMMY_DATA) {
    return DUMMY_WHITELIST.map(w => ({
      badge: w.badge, reason: w.reason,
      added_at: w.added_at.toISOString(),
    }));
  }
  return await apiGet('/api/whitelist');
}

async function addWhitelist(badge, reason) {
  if (USE_DUMMY_DATA) {
    DUMMY_WHITELIST.unshift({ badge, reason, added_at: new Date() });
    return { ok: true };
  }
  return await apiPost('/api/whitelist', { badge, reason });
}

async function removeWhitelist(badge) {
  if (USE_DUMMY_DATA) {
    const i = DUMMY_WHITELIST.findIndex(w => w.badge === badge);
    if (i >= 0) DUMMY_WHITELIST.splice(i, 1);
    return { ok: true };
  }
  return await apiDelete(`/api/whitelist/${encodeURIComponent(badge)}`);
}

async function ackAlertAPI(alertId, ackedBy = 'operator') {
  if (USE_DUMMY_DATA) return { ok: true };
  return await apiPost(`/api/alerts/${alertId}/ack`, { acked_by: ackedBy });
}

/* ─── Dummy CSV (offline fallback) ─── */
function csvFromDummy() {
  const header = 'id,timestamp,badge,camera,type,filename\n';
  const rows = DUMMY_DETECTIONS.map(d =>
    `${d.id},${d.timestamp.toISOString()},${d.badge},${d.camera},${d.type},"${d.filename}"`
  ).join('\n');
  const blob = new Blob([header + rows], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `trinetra-detections-dummy.csv`; a.click();
  URL.revokeObjectURL(url);
}
