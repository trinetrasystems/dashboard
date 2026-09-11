/* ────────────────────────────────────────────────────────────────────────────
 * LOGS & CHARTS PAGE — calls real API via api.js
 * ──────────────────────────────────────────────────────────────────────────── */

/* allTime: the Logs page browses the complete history (today AND earlier).
   Picking a date or month narrows it; clearing them returns to all time. */
const filters = { search: '', date: '', month: '', badge: '', camera: '', zone: '',
                  allTime: true };

/* The table is served one page at a time; charts use /api/detections/summary
   so they still describe the WHOLE filter set, not just the visible page. */
const PAGE_SIZE = 20;
let currentPage = 0;
let lastPage = 0;
let cachedSummary = null;
let cachedDetections = [];
let cachedStats = null;
let perimeterRules = null;

/* Zone presentation — mirrors the Live page */
const ZONE_TAG = {
  breach:  { cls: 'breach', label: 'ALERT REGION' },
  outside: { cls: 'warn',   label: 'OUTSIDE' },
  inside:  { cls: 'ok',     label: 'INSIDE' },
  unknown: { cls: 'muted',  label: 'UNZONED' },
};

/* Counted outside classes, e.g. "Outside-person-3-object-2" */
const COUNTED_RE = /^outside-person-(\d+)-object-(\d+)$/;

function normBadge(b) {
  return String(b || '').trim().toLowerCase().replace(/_/g, '-').replace('boundary', 'boundry');
}
function countsOf(badge) {
  const m = COUNTED_RE.exec(normBadge(badge));
  return m ? { persons: +m[1], objects: +m[2] } : null;
}

/* What rule applies to this class, in words */
function ruleTextFor(zone, badge) {
  if (zone === 'breach') return 'instant';
  if (!perimeterRules) return '—';
  const k = normBadge(badge);
  const rules = perimeterRules.dwell_rules || {};
  if (Object.prototype.hasOwnProperty.call(rules, k)) return `${rules[k]} min dwell`;
  // A counted class inherits the outside-person rule, matching the backend.
  if (COUNTED_RE.test(k)) return `${rules['outside-person'] || 10} min dwell`;
  if ((perimeterRules.inside_badges || []).includes(k)) return 'no alarm';
  return `${perimeterRules.default_dwell_minutes} min dwell`;
}

async function refreshAll(resetPage = false) {
  try {
    if (resetPage) currentPage = 0;
    if (!perimeterRules) perimeterRules = await getPerimeterRules();

    const [summary, stats] = await Promise.all([
      getDetectionsSummary(filters),
      getStats(),
    ]);
    cachedSummary = summary;
    cachedStats = stats;

    const total = summary.total || 0;
    lastPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);
    if (currentPage > lastPage) currentPage = lastPage;

    cachedDetections = await getDetections({
      ...filters, limit: PAGE_SIZE, offset: currentPage * PAGE_SIZE,
    });

    renderLogTable();
    renderPager(total);
    renderCharts();
  } catch (e) {
    console.error('refresh failed:', e);
  }
}

/* Jump to a page — clamped, and only refetches the table slice */
async function gotoPage(n) {
  const target = Math.max(0, Math.min(n, lastPage));
  if (target === currentPage) return;
  currentPage = target;
  try {
    cachedDetections = await getDetections({
      ...filters, limit: PAGE_SIZE, offset: currentPage * PAGE_SIZE,
    });
    renderLogTable();
    renderPager(cachedSummary ? cachedSummary.total : 0);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    console.error('page load failed:', e);
  }
}
window.gotoPage = gotoPage;

function renderPager(total) {
  const status = document.getElementById('pager-status');
  const pager = document.getElementById('log-pager');
  if (!status || !pager) return;

  if (total === 0) {
    pager.style.display = 'none';
    return;
  }
  pager.style.display = 'flex';

  const from = currentPage * PAGE_SIZE + 1;
  const to = Math.min(total, (currentPage + 1) * PAGE_SIZE);
  status.textContent = `${from}–${to} of ${total} · page ${currentPage + 1} of ${lastPage + 1}`;

  const atStart = currentPage === 0;
  const atEnd = currentPage >= lastPage;
  document.getElementById('pager-first').disabled = atStart;
  document.getElementById('pager-prev').disabled = atStart;
  document.getElementById('pager-next').disabled = atEnd;
  document.getElementById('pager-last').disabled = atEnd;
}

function initFilters() {
  // Populate badge + camera dropdowns from /api/stats (uses today's stats — could fetch full list)
  // For now, derive from current detections + stats
  populateDropdowns();

  document.getElementById('filter-search').addEventListener('input', e => {
    filters.search = e.target.value; refreshAll(true);
  });
  document.getElementById('filter-date').addEventListener('change', e => {
    filters.date = e.target.value;
    if (e.target.value) document.getElementById('filter-month').value = '';
    filters.month = document.getElementById('filter-month').value;
    refreshAll(true);
  });
  document.getElementById('filter-month').addEventListener('change', e => {
    filters.month = e.target.value;
    if (e.target.value) document.getElementById('filter-date').value = '';
    filters.date = document.getElementById('filter-date').value;
    refreshAll(true);
  });
  document.getElementById('filter-badge').addEventListener('change', e => {
    filters.badge = e.target.value; refreshAll(true);
  });
  document.getElementById('filter-camera').addEventListener('change', e => {
    filters.camera = e.target.value; refreshAll(true);
  });
  document.getElementById('filter-zone').addEventListener('change', e => {
    filters.zone = e.target.value; refreshAll(true);
  });
  document.getElementById('export-btn').addEventListener('click', () => {
    exportDetectionsCSV(filters);
  });
}

async function populateDropdowns() {
  // Month dropdown — current + 5 prior months
  const monthSel = document.getElementById('filter-month');
  if (monthSel.options.length <= 1) {
    const months = [];
    for (let i = 0; i < 6; i++) {
      const d = new Date(); d.setMonth(d.getMonth() - i);
      const v = d.toISOString().slice(0, 7);
      const l = `${d.toLocaleString('default', { month: 'long' })} ${d.getFullYear()}`;
      months.push({ v, l });
    }
    monthSel.innerHTML = `<option value="">Any month</option>` +
      months.map(m => `<option value="${m.v}">${m.l}</option>`).join('');
  }

  // Badge dropdown — full list from /api/badges (classes.txt)
  const badgeSel = document.getElementById('filter-badge');
  if (badgeSel.options.length <= 1) {
    try {
      const b = await getBadges();
      badgeSel.innerHTML = `<option value="">All classes</option>` +
        (b.badges || []).map(x => `<option value="${escapeHtml(x.badge)}">${escapeHtml(x.badge)}</option>`).join('');
    } catch (e) { console.error('badges fetch failed:', e); }
  }

  // Camera dropdown — populated from stats (today's cameras)
  const camSel = document.getElementById('filter-camera');
  if (camSel.options.length <= 1 && cachedStats && cachedStats.by_camera.length) {
    camSel.innerHTML = `<option value="">All cameras</option>` +
      cachedStats.by_camera.map(([c]) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  }
}

function renderLogTable() {
  const tbody = document.getElementById('log-body');
  const total = cachedSummary ? cachedSummary.total : cachedDetections.length;
  document.getElementById('log-meta').textContent =
    `${total} detection${total === 1 ? '' : 's'} · ${PAGE_SIZE} per page`;
  if (cachedDetections.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;padding:30px;color:var(--text-dim);">no detections match filters</td></tr>`;
    return;
  }
  tbody.innerHTML = cachedDetections.map(d => {
    const ts = new Date(d.timestamp.endsWith('Z') ? d.timestamp : d.timestamp + 'Z');
    const zone = d.zone || 'unknown';
    const zt = ZONE_TAG[zone] || ZONE_TAG.unknown;
    return `
      <tr class="clickable ${zone === 'breach' ? 'alert-row' : ''}" onclick='showLog(${JSON.stringify(d).replace(/'/g, "&#39;")})'>
        <td><div class="thumb">${thumbSVG(d.badge)}</div></td>
        <td class="mono">${fmtTime(ts)} ${ts.toDateString() !== new Date().toDateString() ? '<span style="color:var(--text-dim)">· ' + fmtDate(ts) + '</span>' : ''}</td>
        <td class="mono"><b>${escapeHtml(d.badge)}</b>${(() => {
          const c = d.counts || countsOf(d.badge);
          return c ? `<span class="count-chip">👤 ${c.persons} · 📦 ${c.objects}</span>` : '';
        })()}</td>
        <td><span class="zone-tag ${zt.cls}">${zt.label}</span></td>
      </tr>
    `;
  }).join('');
  populateDropdowns();
}

function thumbSVG(badge) {
  const hash = [...badge].reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0);
  const hue = Math.abs(hash) % 360;
  return `<svg viewBox="0 0 56 36" preserveAspectRatio="none">
    <rect width="56" height="36" fill="hsl(${hue},25%,28%)" opacity="0.4"/>
    <circle cx="22" cy="18" r="6" fill="hsl(${hue},65%,55%)" opacity="0.9"/>
    <rect x="32" y="10" width="14" height="20" rx="2" fill="hsl(${(hue + 60) % 360},55%,55%)" opacity="0.7"/>
  </svg>`;
}

function showLog(d) {
  const zone = d.zone || 'unknown';
  openModal({
    title: d.badge,
    sub: `${d.zone_label || zone} · ${d.camera} · ${fmtTime(new Date(d.timestamp))} · ${ruleTextFor(zone, d.badge)}` +
         (() => { const c = d.counts || countsOf(d.badge); return c ? ` · ${c.persons} person${c.persons === 1 ? '' : 's'} · ${c.objects} object${c.objects === 1 ? '' : 's'}` : ''; })(),
    filename: d.filename,
    hasImage: d.has_image,
  });
}
window.showLog = showLog;

function renderCharts() {
  if (!cachedStats) return;

  // 1. Detections over time
  document.getElementById('chart-timeline').innerHTML = lineChartSVG(cachedStats.daily_30d || []);
  document.getElementById('chart-timeline-sub').textContent =
    `last 30 days · ${(cachedStats.daily_30d || []).reduce((a, b) => a + b, 0)} total`;

  const sum = cachedSummary || { total: 0, by_badge: [], by_camera: [], badge_zones: {} };

  // 2. Detections by class — whole filter set, bars coloured by zone
  const badgeData = (sum.by_badge || []).slice(0, 7);
  const zoneOfBadge = sum.badge_zones || {};
  const ZONE_COLOR = {
    breach: 'var(--red)', outside: 'var(--amber)',
    inside: 'var(--green)', unknown: 'var(--cyan)',
  };
  document.getElementById('chart-by-badge').innerHTML =
    hbarChartSVG(badgeData, d => ZONE_COLOR[zoneOfBadge[d] || 'unknown']);
  document.getElementById('chart-by-badge-sub').textContent =
    `${sum.total} events · coloured by zone`;

  // 3. Detections by camera — whole filter set
  const camData = (sum.by_camera || []).slice(0, 8);
  document.getElementById('chart-by-camera').innerHTML = vbarChartSVG(camData, 'var(--violet)');
  document.getElementById('chart-by-camera-sub').textContent = `${sum.total} events`;

  // 4. Alerts — breach vs early warning, stacked per day
  const breach = cachedStats.breach_30d || [];
  const warn = cachedStats.warning_30d || [];
  document.getElementById('chart-loitering').innerHTML = alertStackSVG(breach, warn);
  const bTotal = breach.reduce((a, b) => a + b, 0);
  const wTotal = warn.reduce((a, b) => a + b, 0);
  document.getElementById('chart-loitering-sub').textContent =
    `last 30 days · ${bTotal} breach · ${wTotal} early warning`;
}

function lineChartSVG(data) {
  if (data.length === 0) return `<div style="text-align:center;color:var(--text-dim);padding:30px;font-family:JetBrains Mono;font-size:12px;">no data</div>`;
  const max = Math.max(...data, 1);
  const pts = data.map((v, i) => {
    const x = 50 + i * 15;
    const y = 160 - (v / max) * 130;
    return `${x},${y}`;
  });
  const line = pts.join(' ');
  const lastX = 50 + (data.length - 1) * 15;
  const area = `50,160 ${line} ${lastX},160`;
  return `<svg viewBox="0 0 500 180" width="100%" style="display: block;">
    <line x1="50" y1="160" x2="${lastX + 5}" y2="160" stroke="var(--border)"/>
    <line x1="50" y1="95"  x2="${lastX + 5}" y2="95"  stroke="var(--border)" stroke-dasharray="2 4" opacity="0.5"/>
    <line x1="50" y1="30"  x2="${lastX + 5}" y2="30"  stroke="var(--border)" stroke-dasharray="2 4" opacity="0.5"/>
    <polygon points="${area}" fill="var(--cyan)" opacity="0.15"/>
    <polyline points="${line}" fill="none" stroke="var(--cyan)" stroke-width="2"/>
    <text x="40" y="34" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">${max}</text>
    <text x="40" y="99" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">${Math.floor(max / 2)}</text>
    <text x="40" y="164" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">0</text>
    <text x="50" y="178" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">30d ago</text>
    <text x="${lastX}" y="178" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">today</text>
  </svg>`;
}

function hbarChartSVG(data, color) {
  if (data.length === 0) return `<div style="text-align:center;color:var(--text-dim);padding:30px;font-family:JetBrains Mono;font-size:12px;">no data</div>`;
  const max = Math.max(...data.map(d => d[1]), 1);
  const colorFor = typeof color === 'function' ? color : () => color;
  let out = '';
  data.forEach(([name, v], i) => {
    const y = 20 + i * 28;
    const w = (v / max) * 320;
    out += `<text x="0" y="${y + 10}" font-family="JetBrains Mono" font-size="12" fill="var(--text-muted)">${escapeHtml(name)}</text>`;
    out += `<rect x="150" y="${y}" width="${w}" height="18" rx="3" fill="${colorFor(name)}" opacity="0.85"/>`;
    out += `<text x="${150 + w + 6}" y="${y + 13}" font-family="JetBrains Mono" font-size="12" font-weight="600" fill="var(--text)">${v}</text>`;
  });
  return `<svg viewBox="0 0 500 ${30 + data.length * 28}" width="100%" style="display: block;">${out}</svg>`;
}

function vbarChartSVG(data, color) {
  if (data.length === 0) return `<div style="text-align:center;color:var(--text-dim);padding:30px;font-family:JetBrains Mono;font-size:12px;">no data</div>`;
  const max = Math.max(...data.map(d => d[1]), 1);
  const barW = 60; const gap = 40; const x0 = 50;
  let out = `<line x1="${x0}" y1="160" x2="${x0 + data.length * (barW + gap)}" y2="160" stroke="var(--border)"/>`;
  data.forEach(([name, v], i) => {
    const h = (v / max) * 130;
    const x = x0 + i * (barW + gap);
    const y = 160 - h;
    out += `<rect x="${x}" y="${y}" width="${barW}" height="${h}" rx="3" fill="${color}" opacity="0.85"/>`;
    out += `<text x="${x + barW / 2}" y="${y - 6}" text-anchor="middle" font-family="JetBrains Mono" font-size="12" font-weight="600" fill="var(--text)">${v}</text>`;
    out += `<text x="${x + barW / 2}" y="178" text-anchor="middle" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">${escapeHtml(name)}</text>`;
  });
  return `<svg viewBox="0 0 500 200" width="100%" style="display: block;">${out}</svg>`;
}

function alertStackSVG(breach, warn) {
  const n = Math.max(breach.length, warn.length);
  if (n === 0) return `<div style="text-align:center;color:var(--text-dim);padding:30px;font-family:JetBrains Mono;font-size:12px;">no data</div>`;
  const totals = [];
  for (let i = 0; i < n; i++) totals.push((breach[i] || 0) + (warn[i] || 0));
  const max = Math.max(...totals, 1);

  let bars = '';
  for (let i = 0; i < n; i++) {
    const x = 50 + i * 15;
    const wH = ((warn[i] || 0) / max) * 130;
    const bH = ((breach[i] || 0) / max) * 130;
    // early warnings sit at the base, breaches stack on top
    if (wH > 0) bars += `<rect x="${x}" y="${160 - wH}" width="11" height="${wH}" rx="2" fill="var(--amber)" opacity="0.9"/>`;
    if (bH > 0) bars += `<rect x="${x}" y="${160 - wH - bH}" width="11" height="${bH}" rx="2" fill="var(--red)" opacity="0.95"/>`;
  }

  return `<svg viewBox="0 0 500 200" width="100%" style="display: block;">
    <line x1="50" y1="160" x2="485" y2="160" stroke="var(--border)"/>
    ${bars}
    <text x="40" y="34" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">${max}</text>
    <text x="40" y="164" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">0</text>
    <rect x="50" y="182" width="9" height="9" rx="2" fill="var(--red)"/>
    <text x="64" y="190" font-family="JetBrains Mono" font-size="11" fill="var(--text-muted)">breach</text>
    <rect x="120" y="182" width="9" height="9" rx="2" fill="var(--amber)"/>
    <text x="134" y="190" font-family="JetBrains Mono" font-size="11" fill="var(--text-muted)">early warning</text>
    <text x="485" y="190" text-anchor="end" font-family="JetBrains Mono" font-size="11" fill="var(--text-dim)">today</text>
  </svg>`;
}

document.addEventListener('DOMContentLoaded', async () => {
  bootstrap('logs');
  initFilters();
  await refreshAll();
  setInterval(refreshAll, 10000);   // refresh log every 10s
});
