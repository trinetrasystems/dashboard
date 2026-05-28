/* ────────────────────────────────────────────────────────────────────────────
 * LOGS & CHARTS PAGE — calls real API via api.js
 * ──────────────────────────────────────────────────────────────────────────── */

const filters = { search: '', date: '', month: '', badge: '', camera: '' };
let cachedDetections = [];
let cachedStats = null;

async function refreshAll() {
  try {
    [cachedDetections, cachedStats] = await Promise.all([
      getDetections(filters),
      getStats(),
    ]);
    renderLogTable();
    renderCharts();
  } catch (e) {
    console.error('refresh failed:', e);
  }
}

function initFilters() {
  // Populate badge + camera dropdowns from /api/stats (uses today's stats — could fetch full list)
  // For now, derive from current detections + stats
  populateDropdowns();

  document.getElementById('filter-search').addEventListener('input', e => {
    filters.search = e.target.value; refreshAll();
  });
  document.getElementById('filter-date').addEventListener('change', e => {
    filters.date = e.target.value;
    if (e.target.value) document.getElementById('filter-month').value = '';
    filters.month = document.getElementById('filter-month').value;
    refreshAll();
  });
  document.getElementById('filter-month').addEventListener('change', e => {
    filters.month = e.target.value;
    if (e.target.value) document.getElementById('filter-date').value = '';
    filters.date = document.getElementById('filter-date').value;
    refreshAll();
  });
  document.getElementById('filter-badge').addEventListener('change', e => {
    filters.badge = e.target.value; refreshAll();
  });
  document.getElementById('filter-camera').addEventListener('change', e => {
    filters.camera = e.target.value; refreshAll();
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
      badgeSel.innerHTML = `<option value="">All badges</option>` +
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
  document.getElementById('log-meta').textContent = `showing ${cachedDetections.length}`;
  if (cachedDetections.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:30px;color:var(--text-dim);">no detections match filters</td></tr>`;
    return;
  }
  tbody.innerHTML = cachedDetections.map(d => {
    const ts = new Date(d.timestamp.endsWith('Z') ? d.timestamp : d.timestamp + 'Z');
    return `
      <tr class="clickable" onclick='showLog(${JSON.stringify(d).replace(/'/g, "&#39;")})'>
        <td><div class="thumb">${thumbSVG(d.badge)}</div></td>
        <td class="mono">${fmtTime(ts)} ${ts.toDateString() !== new Date().toDateString() ? '<span style="color:var(--text-dim)">· ' + fmtDate(ts) + '</span>' : ''}</td>
        <td class="mono"><b>${escapeHtml(d.badge)}</b></td>
        <td class="mono">${escapeHtml(d.camera)}</td>
        <td><span class="tag-${d.type}">${d.type.toUpperCase()}</span></td>
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
  openModal({
    title: d.badge,
    sub: `${d.camera} · ${fmtTime(new Date(d.timestamp))} · ${d.type.toUpperCase()}`,
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

  // 2. Detections by badge — from filtered detections
  const byBadge = {};
  for (const d of cachedDetections) byBadge[d.badge] = (byBadge[d.badge] || 0) + 1;
  const badgeData = Object.entries(byBadge).sort((a, b) => b[1] - a[1]).slice(0, 7);
  document.getElementById('chart-by-badge').innerHTML = hbarChartSVG(badgeData, 'var(--cyan)');
  document.getElementById('chart-by-badge-sub').textContent =
    `${cachedDetections.length} events · top ${badgeData.length}`;

  // 3. Detections by camera — from filtered
  const byCam = {};
  for (const d of cachedDetections) byCam[d.camera] = (byCam[d.camera] || 0) + 1;
  const camData = Object.entries(byCam).sort((a, b) => b[1] - a[1]);
  document.getElementById('chart-by-camera').innerHTML = vbarChartSVG(camData, 'var(--violet)');
  document.getElementById('chart-by-camera-sub').textContent = `${cachedDetections.length} events`;

  // 4. Loitering incidents — from stats
  const loiter = cachedStats.loitering_30d || [];
  document.getElementById('chart-loitering').innerHTML = loiterBarSVG(loiter);
  document.getElementById('chart-loitering-sub').textContent =
    `last 30 days · ${loiter.reduce((a, b) => a + b, 0)} total`;
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
    <text x="40" y="34" text-anchor="end" font-family="JetBrains Mono" font-size="9" fill="var(--text-dim)">${max}</text>
    <text x="40" y="99" text-anchor="end" font-family="JetBrains Mono" font-size="9" fill="var(--text-dim)">${Math.floor(max / 2)}</text>
    <text x="40" y="164" text-anchor="end" font-family="JetBrains Mono" font-size="9" fill="var(--text-dim)">0</text>
    <text x="50" y="178" font-family="JetBrains Mono" font-size="9" fill="var(--text-dim)">30d ago</text>
    <text x="${lastX}" y="178" text-anchor="end" font-family="JetBrains Mono" font-size="9" fill="var(--text-dim)">today</text>
  </svg>`;
}

function hbarChartSVG(data, color) {
  if (data.length === 0) return `<div style="text-align:center;color:var(--text-dim);padding:30px;font-family:JetBrains Mono;font-size:12px;">no data</div>`;
  const max = Math.max(...data.map(d => d[1]), 1);
  let out = '';
  data.forEach(([name, v], i) => {
    const y = 20 + i * 28;
    const w = (v / max) * 320;
    out += `<text x="0" y="${y + 10}" font-family="JetBrains Mono" font-size="10" fill="var(--text-muted)">${escapeHtml(name)}</text>`;
    out += `<rect x="150" y="${y}" width="${w}" height="18" rx="3" fill="${color}" opacity="0.85"/>`;
    out += `<text x="${150 + w + 6}" y="${y + 13}" font-family="JetBrains Mono" font-size="10" font-weight="600" fill="var(--text)">${v}</text>`;
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
    out += `<text x="${x + barW / 2}" y="${y - 6}" text-anchor="middle" font-family="JetBrains Mono" font-size="10" font-weight="600" fill="var(--text)">${v}</text>`;
    out += `<text x="${x + barW / 2}" y="178" text-anchor="middle" font-family="JetBrains Mono" font-size="9" fill="var(--text-dim)">${escapeHtml(name)}</text>`;
  });
  return `<svg viewBox="0 0 500 200" width="100%" style="display: block;">${out}</svg>`;
}

function loiterBarSVG(data) {
  if (data.length === 0) return `<div style="text-align:center;color:var(--text-dim);padding:30px;font-family:JetBrains Mono;font-size:12px;">no data</div>`;
  const max = Math.max(...data, 1);
  let bars = '';
  data.forEach((v, i) => {
    if (v === 0) return;
    const h = (v / max) * 130;
    const x = 50 + i * 15;
    const y = 160 - h;
    bars += `<rect x="${x}" y="${y}" width="11" height="${h}" rx="2" fill="var(--red)" opacity="0.85"/>`;
  });
  return `<svg viewBox="0 0 500 200" width="100%" style="display: block;">
    <line x1="50" y1="160" x2="485" y2="160" stroke="var(--border)"/>
    ${bars}
    <text x="40" y="34" text-anchor="end" font-family="JetBrains Mono" font-size="9" fill="var(--text-dim)">${max}</text>
    <text x="40" y="164" text-anchor="end" font-family="JetBrains Mono" font-size="9" fill="var(--text-dim)">0</text>
  </svg>`;
}

document.addEventListener('DOMContentLoaded', async () => {
  bootstrap('logs');
  initFilters();
  await refreshAll();
  setInterval(refreshAll, 10000);   // refresh log every 10s
});
