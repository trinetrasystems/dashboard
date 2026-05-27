/* ────────────────────────────────────────────────────────────────────────────
 * DUMMY DATA — replace with real API calls when backend is ready.
 * All data is computed relative to "now" so durations and timestamps look live.
 * ──────────────────────────────────────────────────────────────────────────── */

const NOW = new Date();
function minutesAgo(m, s = 0) {
  return new Date(NOW.getTime() - m * 60000 - s * 1000);
}
function hoursAgo(h, m = 0) {
  return new Date(NOW.getTime() - h * 3600000 - m * 60000);
}
function daysAgo(d) {
  return new Date(NOW.getTime() - d * 86400000);
}

/* ─── Active premises sessions ─── */
const DUMMY_SESSIONS = [
  // LOITERING (>15min) — whitelisted: ignored for alerts
  {
    id: 1, badge: 'WHITE-CROSS',
    first_seen: minutesAgo(45, 3),
    last_seen: minutesAgo(0, 6),
    total_sightings: 280,
    last_camera: 'cam-entrance',
    whitelisted: true,
  },
  // LOITERING (>15min) — active alert
  {
    id: 2, badge: 'CYAN-PENTAGON',
    first_seen: minutesAgo(18, 43),
    last_seen: minutesAgo(0, 2),
    total_sightings: 112,
    last_camera: 'cam-parking',
    whitelisted: false,
  },
  {
    id: 3, badge: 'AMBER-TRIANGLE',
    first_seen: minutesAgo(16, 9),
    last_seen: minutesAgo(0, 5),
    total_sightings: 88,
    last_camera: 'cam-entrance',
    whitelisted: false,
  },
  // LOITERING but acknowledged
  {
    id: 4, badge: 'BLUE-SQUARE',
    first_seen: minutesAgo(15, 42),
    last_seen: minutesAgo(0, 16),
    total_sightings: 75,
    last_camera: 'cam-entrance',
    whitelisted: false,
    acked: true, acked_by: 'guard', acked_at: minutesAgo(8),
  },
  // APPROACHING (5-15min)
  {
    id: 5, badge: 'YELLOW-STAR',
    first_seen: minutesAgo(12),
    last_seen: minutesAgo(0, 10),
    total_sightings: 45,
    last_camera: 'cam-gate',
    whitelisted: false,
  },
  {
    id: 6, badge: 'ORANGE-DIAMOND',
    first_seen: minutesAgo(8, 36),
    last_seen: minutesAgo(0, 16),
    total_sightings: 33,
    last_camera: 'cam-entrance',
    whitelisted: false,
  },
  // OK (<5min)
  {
    id: 7, badge: 'VIOLET-HEXAGON',
    first_seen: minutesAgo(4, 7),
    last_seen: minutesAgo(0, 1),
    total_sightings: 18,
    last_camera: 'cam-parking',
    whitelisted: false,
  },
  {
    id: 8, badge: 'RED-CIRCLE',
    first_seen: minutesAgo(2, 28),
    last_seen: minutesAgo(0, 1),
    total_sightings: 12,
    last_camera: 'cam-entrance',
    whitelisted: false,
  },
  {
    id: 9, badge: 'GREEN-TRIANGLE',
    first_seen: minutesAgo(1, 17),
    last_seen: minutesAgo(0, 7),
    total_sightings: 5,
    last_camera: 'cam-gate',
    whitelisted: false,
  },
];

/* ─── Detection log (today + earlier) ─── */
const DUMMY_DETECTIONS = (() => {
  const items = [];
  const badges = [
    'RED-CIRCLE','CYAN-PENTAGON','AMBER-TRIANGLE','VIOLET-HEXAGON',
    'GREEN-TRIANGLE','BLUE-SQUARE','YELLOW-STAR','WHITE-CROSS',
    'ORANGE-DIAMOND','PINK-OCTAGON','BLACK-SHIELD','GOLD-STAR',
  ];
  const cameras = ['cam-entrance','cam-parking','cam-gate','cam-perimeter'];
  const exitCams = new Set(['cam-gate']);  // exit-gate cameras

  // Today: 50 events
  for (let i = 0; i < 50; i++) {
    const ts = new Date(NOW.getTime() - i * 90 * 1000 - Math.random() * 60000);
    const cam = cameras[Math.floor(Math.random() * cameras.length)];
    const badge = badges[Math.floor(Math.random() * badges.length)];
    let type = 'sighting';
    if (i % 7 === 0) type = exitCams.has(cam) ? 'exit' : 'entry';
    items.push({
      id: 10000 - i,
      filename: `${cam}_${badge}_${ts.toISOString().slice(0,19).replace(/[-:T]/g,'').replace(/(\d{8})(\d{6})/,'$1-$2')}.jpg`,
      camera: cam,
      badge,
      timestamp: ts,
      type,
      has_image: Math.random() > 0.1,  // 90% have archived image
    });
  }
  // Earlier days: scatter ~30 more across past 30 days
  for (let i = 0; i < 30; i++) {
    const ts = daysAgo(1 + Math.floor(Math.random() * 29));
    ts.setHours(Math.floor(Math.random() * 24));
    const cam = cameras[Math.floor(Math.random() * cameras.length)];
    const badge = badges[Math.floor(Math.random() * badges.length)];
    items.push({
      id: 9000 - i,
      filename: `${cam}_${badge}.jpg`,
      camera: cam, badge, timestamp: ts,
      type: i % 5 === 0 ? 'exit' : 'sighting',
      has_image: Math.random() > 0.3,
    });
  }
  return items.sort((a, b) => b.timestamp - a.timestamp);
})();

/* ─── Whitelist ─── */
const DUMMY_WHITELIST = [
  { badge: 'WHITE-CROSS',     reason: 'security guard · day shift',  added_at: daysAgo(5) },
  { badge: 'BLACK-SHIELD',    reason: 'facility manager',            added_at: daysAgo(7) },
  { badge: 'GOLD-STAR',       reason: 'CEO',                         added_at: daysAgo(10) },
  { badge: 'SILVER-DIAMOND',  reason: 'cleaning staff · evening',    added_at: daysAgo(13) },
];

/* ─── Stats (computed from above) ─── */
function dummyStats() {
  const todayStart = new Date(NOW); todayStart.setHours(0, 0, 0, 0);
  const today = DUMMY_DETECTIONS.filter(d => d.timestamp >= todayStart);

  const byBadge = {};
  const byCamera = {};
  for (const d of today) {
    byBadge[d.badge] = (byBadge[d.badge] || 0) + 1;
    byCamera[d.camera] = (byCamera[d.camera] || 0) + 1;
  }
  // Hourly buckets
  const hourly = new Array(24).fill(0);
  for (const d of today) hourly[d.timestamp.getHours()]++;

  // Last 30 days totals
  const dailyTotals = new Array(30).fill(0);
  for (const d of DUMMY_DETECTIONS) {
    const ago = Math.floor((NOW - d.timestamp) / 86400000);
    if (ago >= 0 && ago < 30) dailyTotals[29 - ago]++;
  }

  return {
    total_today: today.length,
    active_sessions: DUMMY_SESSIONS.length,
    loitering_count: DUMMY_SESSIONS.filter(s =>
      !s.whitelisted && !s.acked && (NOW - s.first_seen)/60000 >= 15
    ).length,
    jetsons_online: 3,
    jetsons_total: 4,
    by_badge: Object.entries(byBadge).sort((a,b)=>b[1]-a[1]).slice(0, 7),
    by_camera: Object.entries(byCamera).sort((a,b)=>b[1]-a[1]),
    hourly_today: hourly,
    daily_30d: dailyTotals,
    loitering_30d: [0,0,1,0,0,2,0,1,0,3,0,0,1,2,0,0,4,2,1,0,0,3,0,1,2,0,0,1,3,2],
  };
}
