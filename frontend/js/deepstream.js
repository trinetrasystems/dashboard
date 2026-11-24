/* ────────────────────────────────────────────────────────────────────────────
 * DEEPSTREAM — the perimeter pipeline (Trinetra team): Pipeline (start / stop / engine / settings / log),
 * Cameras (RTSP or a test video, connection test), Perimeter zones (drawn on the camera's picture) and Live view.
 * Zones are fractions of the frame; the outside-side rule below is the same as backend/pipeline/zones.py.
 * ──────────────────────────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
const S = { tab: 'pipeline', cfg: null, status: null, logKind: 'pipeline', editing: null,
            zcam: null, zone: null, draft: null, tool: null, hist: [], dirty: false, hover: null };
const COL = { line: '#ffb020', alert: '#ff4d5e', ignore: '#8a8f99', draft: '#4ecdc4' };

/* ─── helpers ─── */
const maskUri = (u) => String(u || '').replace(/\/\/([^:@/]+):([^@/]+)@/, '//$1:•••••@');
const hhmm = (iso) => { if (!iso) return ''; const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
function msg(id, text, ok) { const el = $(id); el.className = `form-result ${ok ? 'good' : 'bad'}`; el.textContent = text; }

/* ════════ tabs ════════ */
function showTab(t) {
  S.tab = t;
  document.querySelectorAll('#ds-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  ['pipeline', 'cameras', 'zones', 'live'].forEach(x => { $(`tab-${x}`).hidden = x !== t; });
  history.replaceState(null, '', `#${t}${t === 'zones' && S.zcam ? '=' + S.zcam : ''}`);
  if (t === 'zones') openZones(S.zcam);
  if (t === 'live') renderLive();
  if (t === 'pipeline') loadLog();
}

/* ════════ pipeline ════════ */
async function loadStatus() {
  try { S.status = await apiGet('/api/ds/status'); } catch (e) { return; }
  const st = S.status, job = st.job, busy = job && job.status === 'running';
  $('ds-unavailable').hidden = st.available;
  const dot = $('ds-dot'), on = (st.state.streams || []).filter(s => s.online).length;
  let text, sub = '', cls = '';
  if (busy) { cls = 'busy'; text = { start: 'Starting…', stop: 'Stopping…', 'build engine': 'Building the engine…' }[job.action] || 'Working…';
              sub = (job.lines.slice(-1)[0] || '').replace(/^\d\d:\d\d:\d\d /, ''); }
  else if (st.running) { cls = 'run'; text = 'Running'; sub = `since ${hhmm(st.state.started)} · ${on} of ${(st.state.streams || []).length} camera(s) delivering frames · ${st.model || ''}`; }
  else if (st.intent) { cls = 'busy'; text = 'Restarting…'; sub = `stopped (${st.exit_reason || 'dashboard restart'}); started again automatically within a minute`; }
  else { cls = st.exit_reason && !['stop', 'eos'].includes(st.exit_reason) ? 'bad' : ''; text = 'Stopped';
         sub = st.exit_reason && !['stop', 'eos'].includes(st.exit_reason) ? `last run ended: ${st.exit_reason} — see the log` : 'not running'; }
  dot.className = `ds-dot ${cls}`; $('ds-state').textContent = text; $('ds-state-sub').textContent = sub;
  $('ds-start').textContent = st.running ? '↻ Restart' : '▶ Start';
  $('ds-start').disabled = busy || !st.available; $('ds-build').disabled = busy || !st.available;
  $('ds-stop').disabled = !(st.running || busy || st.intent);
  const e = st.engine, note = $('ds-engine-note');
  note.className = 'ds-note';
  if (!st.cameras) { note.className = 'ds-note warn'; note.textContent = 'No camera switched on yet: add one on the Cameras tab, then draw its zones.'; }
  else if (!st.model) { note.className = 'ds-note warn'; note.textContent = 'No detector model found (data folder ds/models: <name>.onnx + <name>.labels.txt).'; }
  else if (e && e.ready) note.textContent = `Engine ready: ${st.model} for up to ${e.size} camera(s).`;
  else if (e) note.textContent = `Start builds the TensorRT engine of ${st.model} for ${e.size} camera(s) first — one time, about ${e.minutes} min.`;
  const missing = (S.cfg?.streams || []).filter(s => s.enabled && !st.zones_drawn.includes(s.name)).map(s => s.name);
  if (st.cameras && missing.length) { note.className = 'ds-note warn'; note.textContent += ` No zones drawn for ${missing.join(', ')}: nothing is reported from ${missing.length > 1 ? 'them' : 'it'} (Perimeter zones tab).`; }
  const rows = st.state.streams || [];
  $('ds-streams').innerHTML = rows.length ? rows.map(s => `<tr>
      <td class="who-name">${escapeHtml(s.name)}<div class="who-sub">${escapeHtml(s.type)}${s.reconnects ? ` · ${s.reconnects} reconnects` : ''}</div></td>
      <td>${s.online ? '<span class="tag on_desk">Online</span>' : `<span class="tag bad">Offline</span>${s.reachable === false ? ' <span class="muted">not reachable</span>' : ''}`}</td>
      <td class="mono">${s.fps ?? 0}</td>
      <td>${Object.entries(s.images || {}).map(([c, n]) => `<span class="tag">${escapeHtml(c)} ${n}</span>`).join('') || '<span class="muted">none</span>'}</td></tr>`).join('')
    : `<tr><td colspan="4" class="empty">${st.running ? 'starting…' : 'not running'}</td></tr>`;
  if (job) { $('ds-job-title').textContent = `${job.action} · ${job.status}${job.error ? ' — ' + job.error : ''}`;
             const pre = $('ds-job'), atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
             pre.textContent = job.lines.join('\n'); if (atEnd) pre.scrollTop = pre.scrollHeight; }
  const h = st.health || {};
  $('ds-health').innerHTML = `<dt>Memory free</dt><dd>${h.mem_available_gb ?? '?'} of ${h.mem_total_gb ?? '?'} GB</dd>
    <dt>Temperature</dt><dd>${h.temp_c ?? '?'} °C</dd><dt>Disk free</dt><dd>${h.free_disk_gb ?? '?'} GB</dd>
    <dt>Images saved</dt><dd>${st.state.images_paused_low_disk ? '<span style="color:var(--red)">paused (disk low)</span>' : 'yes'}</dd>`;
  $('ds-updated').textContent = `updated ${new Date().toLocaleTimeString('en-GB')}`;
}

async function action(what) {
  if (what === 'stop' && !confirm('Stop the perimeter pipeline? No breach or outside alerts until it is started again.')) return;
  try { await apiPost(`/api/ds/${what}`, {}); toast(what === 'start' ? 'Starting the pipeline…' : what === 'stop' ? 'Stopping…' : 'Building the engine…'); }
  catch (e) { toast(e.message); }
  loadStatus();
}

async function loadLog() {
  try {
    const d = await apiGet(`/api/ds/log?kind=${S.logKind}&lines=300`);
    const pre = $('ds-log'), atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = d.text; $('ds-log-file').textContent = d.file || '';
    if (atEnd) pre.scrollTop = pre.scrollHeight;
  } catch (_) { }
}

function fillSettings() {
  const c = S.cfg, f = $('ds-settings').elements;
  $('ds-model').innerHTML = c.models.map(m => `<option value="${escapeHtml(m.name)}">${escapeHtml(m.name)} — ${m.kind}${m.engines.length ? ` · engines for ${m.engines.join(', ')}` : ''}</option>`).join('')
    || '<option value="">no model found</option>';
  f.model.value = c.model || (c.models[0]?.name || '');
  const m = c.models.find(x => x.name === f.model.value);
  $('ds-model-help').textContent = m ? `classes: ${m.labels.join(', ')}${m.kind === 'built-in' ? ' — stand-in until the perimeter model is ready' : ''}` : '';
  f.threshold.value = c.threshold; f.save_every_s.value = c.save_every_s; f.frame_skip.value = c.frame_skip; f.loop_files.checked = c.loop_files;
  f.nightly_restart.value = c.nightly_restart || '';
}

async function saveConfig(patch, msgId) {
  try {
    const out = await apiPut('/api/ds/config', { ...S.cfg, ...patch });
    S.cfg = out;
    if (msgId) msg(msgId, out.restart_needed ? 'Saved — restart the pipeline to use it.' : 'Saved.', true);
    return out;
  } catch (e) { if (msgId) msg(msgId, e.message, false); throw e; }
}

/* ════════ cameras ════════ */
function renderCameras() {
  const st = S.cfg.streams, drawn = S.status?.zones_drawn || [];
  $('cam-count').textContent = st.length;
  $('cam-body').innerHTML = st.length ? st.map((s, i) => `<tr class="${s.enabled ? '' : 'off'}">
      <td class="who-name">${escapeHtml(s.name)}</td>
      <td class="url-cell" title="${escapeHtml(maskUri(s.uri))}">${escapeHtml(maskUri(s.uri))}</td>
      <td class="mono">${s.uri.startsWith('file://') ? 'file' : s.transport.toUpperCase()}</td>
      <td>${s.enabled ? '<span class="tag on_desk">On</span>' : '<span class="tag">Off</span>'}</td>
      <td>${drawn.includes(s.name) ? '<span class="tag on_desk">Drawn</span>' : '<span class="tag bad">Not drawn</span>'}</td>
      <td class="row-actions"><button class="btn sm" data-draw="${i}">Draw zones</button>
        <button class="btn sm" data-edit="${i}">Edit</button><button class="btn sm danger-ghost" data-del="${i}">Remove</button></td></tr>`).join('')
    : '<tr><td colspan="6" class="empty">No cameras yet — “Add camera”.</td></tr>';
}

function openCam(i) {
  S.editing = i;
  const f = $('cam-form').elements, s = i == null ? null : S.cfg.streams[i];
  $('cam-form').reset(); $('cam-msg').textContent = '';
  $('cam-title').textContent = s ? `Edit ${s.name}` : 'Add camera';
  f.name.value = s?.name || ''; f.uri.value = s?.uri || ''; f.transport.value = s?.transport || 'tcp'; f.enabled.checked = s ? s.enabled : true;
  $('cam-sample').innerHTML = '<option value="">—</option>' + (S.cfg.sample_videos || []).map(v => `<option value="file://${escapeHtml(v)}">${escapeHtml(v.split('/').pop())}</option>`).join('');
  $('cam-modal').hidden = false; f.name.focus();
}
const closeCam = () => { $('cam-modal').hidden = true; };

async function testCam() {
  const f = $('cam-form').elements;
  msg('cam-msg', 'Testing the connection (up to 10 s)…', true);
  $('cam-test').disabled = true;
  try {
    const r = await apiPost('/api/ds/probe', { uri: f.uri.value.trim(), transport: f.transport.value });
    if (r.ok) msg('cam-msg', `Connected: ${r.width}×${r.height} ${r.codec}${r.fps ? ` · ${r.fps} fps` : ''}`, true);
    else msg('cam-msg', `No video: ${r.error}`, false);
  } catch (e) { msg('cam-msg', e.message, false); }
  finally { $('cam-test').disabled = false; }
}

async function saveCam(ev) {
  ev.preventDefault();
  const f = $('cam-form').elements;
  const cam = { name: f.name.value.trim(), uri: f.uri.value.trim(), transport: f.transport.value, enabled: f.enabled.checked };
  const streams = S.cfg.streams.slice();
  if (S.editing == null) streams.push(cam); else streams[S.editing] = cam;
  try {
    const out = await saveConfig({ streams });
    closeCam(); renderCameras(); fillZoneCams(); loadStatus();
    toast(out.restart_needed ? `${cam.name} saved — restart the pipeline to use it` : `${cam.name} saved — now draw its zones`);
  } catch (e) { msg('cam-msg', e.message, false); }
}

async function removeCam(i) {
  const s = S.cfg.streams[i];
  if (!confirm(`Remove the camera ${s.name}? Its zones are removed too.`)) return;
  try { await saveConfig({ streams: S.cfg.streams.filter((_, k) => k !== i) }); renderCameras(); fillZoneCams(); loadStatus(); toast(`${s.name} removed`); }
  catch (e) { toast(e.message); }
}

/* ════════ perimeter zones ════════ */
const HELP = {
  null: 'Pick a tool, then click on the picture. Move the pointer over the picture to see what a person standing there would count as.',
  line: '<b>Perimeter line:</b> click along the wall or fence, point by point. <b>Double-click</b> or press <b>Enter</b> to finish. A new line replaces the old one.',
  outside: '<b>Outside side:</b> click once anywhere on the outside of the perimeter line (the arrows then point outside).',
  alert: '<b>Alert region:</b> click the corners of the wall top / concertina wire. Click the <b>first point</b> again or <b>double-click</b> to close it. Anything here is a breach.',
  ignore: '<b>Ignore region:</b> click the corners of an area to ignore (a road, a moving tree). Close it like an alert region.',
};
const emptyZone = () => ({ line: [], outside: null, alert: [], ignore: [] });

function fillZoneCams() {
  const sel = $('zone-cam'), streams = S.cfg.streams;
  sel.innerHTML = streams.map(s => `<option value="${escapeHtml(s.name)}">${escapeHtml(s.name)}${s.enabled ? '' : ' (off)'}</option>`).join('')
    || '<option value="">no cameras</option>';
  if (!S.zcam || !streams.some(s => s.name === S.zcam)) S.zcam = streams[0]?.name || null;
  sel.value = S.zcam || '';
}

async function openZones(cam, refresh = false) {
  if (S.dirty && cam !== S.zcam && !confirm('The zones of this camera are not saved. Leave them?')) { $('zone-cam').value = S.zcam; return; }
  S.zcam = cam || S.zcam; S.draft = null; S.dirty = false; S.hist = []; $('zone-msg').textContent = '';
  if (!S.zcam) { showEmpty('Add a camera on the Cameras tab first.'); S.zone = emptyZone(); draw(); return; }
  try { S.zone = { ...emptyZone(), ...(await apiGet(`/api/ds/zones/${encodeURIComponent(S.zcam)}`)) }; } catch (e) { S.zone = emptyZone(); }
  showEmpty('Taking a picture from the camera…');
  const img = $('zone-img');
  img.onload = () => { $('zone-empty').hidden = true; sizeCanvas(); draw(); };
  img.onerror = async () => {
    let why = 'no picture from this camera';
    try { const r = await fetch(img.src, { credentials: 'same-origin' }); why = (await r.json()).detail || why; } catch (_) { }
    showEmpty(`${why}\nCheck the stream address on the Cameras tab, then “New picture”.`); sizeCanvas(); draw();
  };
  img.src = `/api/ds/frame/${encodeURIComponent(S.zcam)}?refresh=${refresh ? 1 : 0}&t=${Date.now()}`;
  renderZoneList(); draw();
}
function showEmpty(text) { const e = $('zone-empty'); e.hidden = false; e.textContent = text; if (!$('zone-img').complete) $('zone-img').removeAttribute('src'); }

function sizeCanvas() {
  const c = $('zone-canvas'), img = $('zone-img'), r = devicePixelRatio || 1;
  const w = img.clientWidth || $('zone-stage').clientWidth, h = img.clientHeight || Math.round(w * 9 / 16);
  c.width = Math.round(w * r); c.height = Math.round(h * r);
  if (!img.naturalWidth) $('zone-stage').style.minHeight = `${h}px`;
}

/* same rule as zones.py: the side of the closest segment, the end segments extended */
function closestSeg(p, line) {
  let best = null, seg = null;
  for (let i = 0; i < line.length - 1; i++) {
    const a = line[i], b = line[i + 1], dx = b[0] - a[0], dy = b[1] - a[1], L = dx * dx + dy * dy;
    const t = L ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L)) : 0;
    const d = (p[0] - a[0] - t * dx) ** 2 + (p[1] - a[1] - t * dy) ** 2;
    if (best === null || d < best) { best = d; seg = [a, b]; }
  }
  return seg;
}
const sideOf = (p, [a, b]) => (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]);
function isOutside(p, z) {
  if (z.line.length < 2 || !z.outside) return null;
  const so = sideOf(z.outside, closestSeg(z.outside, z.line)), sp = sideOf(p, closestSeg(p, z.line));
  return so !== 0 && sp * so > 0;
}
function inPoly(p, poly) {
  let ins = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > p[1]) !== (yj > p[1]) && p[0] < (xj - xi) * (p[1] - yi) / ((yj - yi) || 1e-12) + xi) ins = !ins;
  }
  return ins;
}
function meaning(p, z) {
  if (z.ignore.some(poly => inPoly(p, poly))) return ['ignored', COL.ignore];
  if (z.alert.some(poly => inPoly(p, poly))) return ['BREACH (alert region)', COL.alert];
  const o = isOutside(p, z);
  if (o === null) return z.alert.length ? ['not reported', '#3ddc97'] : ['draw the perimeter line + outside side', '#aaa'];
  return o ? ['OUTSIDE → warning after the dwell time', COL.line] : ['inside → not reported', '#3ddc97'];
}

function draw() {
  const c = $('zone-canvas'), ctx = c.getContext('2d'), W = c.width, H = c.height, r = devicePixelRatio || 1, z = S.zone || emptyZone();
  ctx.clearRect(0, 0, W, H);
  const P = (p) => [p[0] * W, p[1] * H];
  const poly = (pts, fill, stroke, closed = true, dash = []) => {
    if (!pts.length) return;
    ctx.beginPath(); pts.forEach((p, i) => { const [x, y] = P(p); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    if (closed) ctx.closePath();
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    ctx.setLineDash(dash.map(d => d * r)); ctx.strokeStyle = stroke; ctx.lineWidth = 2.5 * r; ctx.stroke(); ctx.setLineDash([]);
  };
  const dots = (pts, col) => pts.forEach(p => { const [x, y] = P(p); ctx.beginPath(); ctx.arc(x, y, 4.5 * r, 0, 7); ctx.fillStyle = col; ctx.fill();
                                                 ctx.lineWidth = 1.5 * r; ctx.strokeStyle = '#000'; ctx.stroke(); });
  const label = (text, p, col) => { const [x, y] = P(p); ctx.font = `600 ${12.5 * r}px "Open Sans", sans-serif`;
    const w = ctx.measureText(text).width + 10 * r; ctx.fillStyle = 'rgba(0,0,0,.65)'; ctx.fillRect(x - w / 2, y - 10 * r, w, 20 * r);
    ctx.fillStyle = col; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(text, x, y); };
  z.ignore.forEach((pts, i) => { poly(pts, 'rgba(138,143,153,.35)', COL.ignore); label(`ignore ${i + 1}`, centroid(pts), '#ddd'); });
  z.alert.forEach((pts, i) => { poly(pts, 'rgba(255,77,94,.30)', COL.alert); label(`ALERT ${i + 1}`, centroid(pts), '#ff8a95'); });
  if (z.line.length >= 2) {
    poly(z.line, null, COL.line, false); dots(z.line, COL.line);
    if (z.outside) {                                    // arrows towards the outside, on every segment
      for (let i = 0; i < z.line.length - 1; i++) {
        const a = z.line[i], b = z.line[i + 1], m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
        const dxp = (b[0] - a[0]) * W, dyp = (b[1] - a[1]) * H, L = Math.hypot(dxp, dyp) || 1;
        let n = [-dyp / L * 34 * r, dxp / L * 34 * r];     // the segment's normal, 34 px long (canvas pixels)
        const test = [m[0] + n[0] / W * 0.5, m[1] + n[1] / H * 0.5];
        if (!isOutside(test, z)) n = [-n[0], -n[1]];       // point it to the outside
        const [mx, my] = P(m); arrow(ctx, mx, my, mx + n[0], my + n[1], COL.line, r);
      }
    }
  } else if (z.line.length === 1) dots(z.line, COL.line);
  if (z.outside) { const [x, y] = P(z.outside); ctx.beginPath(); ctx.arc(x, y, 7 * r, 0, 7); ctx.fillStyle = COL.line; ctx.fill();
                   label('OUTSIDE', [z.outside[0], Math.max(0.03, z.outside[1] - 0.05)], COL.line); }
  if (S.draft && S.draft.pts.length) {                  // what is being drawn now (+ the segment to the pointer)
    const pts = S.hover ? [...S.draft.pts, S.hover] : S.draft.pts;
    poly(pts, null, COL.draft, false, [6, 4]); dots(S.draft.pts, COL.draft);
  }
}
function arrow(ctx, x1, y1, x2, y2, col, r) {
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.strokeStyle = col; ctx.lineWidth = 2.5 * r; ctx.stroke();
  const a = Math.atan2(y2 - y1, x2 - x1), h = 9 * r;
  ctx.beginPath(); ctx.moveTo(x2, y2); ctx.lineTo(x2 - h * Math.cos(a - 0.5), y2 - h * Math.sin(a - 0.5));
  ctx.lineTo(x2 - h * Math.cos(a + 0.5), y2 - h * Math.sin(a + 0.5)); ctx.closePath(); ctx.fillStyle = col; ctx.fill();
}
const centroid = (pts) => [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];

function renderZoneList() {
  const z = S.zone || emptyZone(), rows = [];
  rows.push(`<div class="zone-item"><span><span class="sw" style="background:${COL.line}"></span>Perimeter line · ${z.line.length >= 2 ? `${z.line.length} points` : '<i>not drawn</i>'}</span>
    ${z.line.length ? '<button class="btn sm danger-ghost" data-zdel="line">✕</button>' : ''}</div>`);
  rows.push(`<div class="zone-item"><span><span class="sw" style="background:${COL.line}"></span>Outside side · ${z.outside ? 'marked' : '<i>not marked</i>'}</span>
    ${z.outside ? '<button class="btn sm danger-ghost" data-zdel="outside">✕</button>' : ''}</div>`);
  z.alert.forEach((p, i) => rows.push(`<div class="zone-item"><span><span class="sw" style="background:${COL.alert}"></span>Alert region ${i + 1} · ${p.length} corners</span>
    <button class="btn sm danger-ghost" data-zdel="alert:${i}">✕</button></div>`));
  z.ignore.forEach((p, i) => rows.push(`<div class="zone-item"><span><span class="sw" style="background:${COL.ignore}"></span>Ignore region ${i + 1} · ${p.length} corners</span>
    <button class="btn sm danger-ghost" data-zdel="ignore:${i}">✕</button></div>`));
  $('zone-list').innerHTML = rows.join('');
}

function setTool(t) {
  finishDraft();
  S.tool = S.tool === t ? null : t;
  document.querySelectorAll('#zone-tools button').forEach(b => b.classList.toggle('active', b.dataset.tool === S.tool));
  $('zone-help').innerHTML = HELP[S.tool];
}
function remember() { S.hist.push(JSON.stringify(S.zone)); if (S.hist.length > 60) S.hist.shift(); S.dirty = true; $('zone-msg').textContent = 'not saved'; $('zone-msg').className = 'form-result bad'; }
function changed() { renderZoneList(); draw(); }

function finishDraft() {
  const d = S.draft; S.draft = null;
  if (!d) return;
  if (d.tool === 'line' && d.pts.length >= 2) { remember(); S.zone.line = d.pts; }
  if ((d.tool === 'alert' || d.tool === 'ignore') && d.pts.length >= 3) { remember(); S.zone[d.tool].push(d.pts); }
  changed();
}
function pointAt(ev) {
  const rc = $('zone-canvas').getBoundingClientRect();
  return [Math.min(1, Math.max(0, (ev.clientX - rc.left) / rc.width)), Math.min(1, Math.max(0, (ev.clientY - rc.top) / rc.height))];
}
function nearPx(a, b) { const rc = $('zone-canvas').getBoundingClientRect(); return Math.hypot((a[0] - b[0]) * rc.width, (a[1] - b[1]) * rc.height); }

function onClick(ev) {
  if (!S.tool || !S.zcam) return;
  const p = pointAt(ev);
  if (S.tool === 'outside') { remember(); S.zone.outside = p; changed(); return; }
  if (!S.draft) S.draft = { tool: S.tool, pts: [] };
  const pts = S.draft.pts;
  if ((S.tool === 'alert' || S.tool === 'ignore') && pts.length >= 3 && nearPx(p, pts[0]) < 12) { finishDraft(); return; }
  if (pts.length && nearPx(p, pts[pts.length - 1]) < 3) return;      // the second click of a double-click
  pts.push(p); draw();
}
function onMove(ev) {
  const p = pointAt(ev); S.hover = S.draft ? p : null;
  if (S.draft) draw();
  if (S.zone) $('zone-pointer').textContent = `Pointer: a person standing here → ${meaning(p, S.zone)[0]}`;   // its own line: nothing moves
}
function undo() {
  if (S.draft && S.draft.pts.length) { S.draft.pts.pop(); if (!S.draft.pts.length) S.draft = null; draw(); return; }
  if (!S.hist.length) return;
  S.zone = JSON.parse(S.hist.pop()); S.dirty = true; changed();
}
async function saveZones() {
  finishDraft();
  if (S.zone.line.length >= 2 && !S.zone.outside) { msg('zone-msg', 'Mark the outside side first (tool “Outside side”).', false); return; }
  try {
    const out = await apiPut(`/api/ds/zones/${encodeURIComponent(S.zcam)}`, S.zone);
    S.zone = { ...emptyZone(), ...out.zone }; S.dirty = false; S.hist = [];
    msg('zone-msg', `Saved — used by the pipeline ${out.applied}.`, true); changed(); loadStatus();
  } catch (e) { msg('zone-msg', e.message, false); }
}

/* ════════ live view ════════ */
function renderLive() {
  const cams = (S.cfg?.streams || []).filter(s => s.enabled), running = S.status?.running;
  $('live-grid').innerHTML = cams.length ? cams.map(s => `<div class="live-cell">
      ${running ? `<img data-live="${escapeHtml(s.name)}" alt="${escapeHtml(s.name)}">` : '<div class="live-off">pipeline stopped — start it on the Pipeline tab</div>'}
      <div class="live-cap"><b>${escapeHtml(s.name)}</b><span class="muted" data-cap="${escapeHtml(s.name)}"></span></div></div>`).join('')
    : '<div class="card-hint">No camera switched on.</div>';
  refreshLive();
}
function refreshLive() {
  if (S.tab !== 'live') return;
  const streams = S.status?.state?.streams || [];
  document.querySelectorAll('[data-live]').forEach(img => {
    const cam = img.dataset.live, n = new Image();
    n.onload = () => { img.src = n.src; };
    n.onerror = () => { img.removeAttribute('src'); };
    n.src = `/api/ds/live/${encodeURIComponent(cam)}?t=${Date.now()}`;
    const st = streams.find(s => s.name === cam), cap = document.querySelector(`[data-cap="${CSS.escape(cam)}"]`);
    if (cap) cap.textContent = st ? `${st.online ? 'online' : 'OFFLINE'} · ${st.fps} fps` : '';
  });
}

/* ════════ start ════════ */
onReady(async () => {
  try { S.cfg = await apiGet('/api/ds/config'); } catch (e) { return; }
  fillSettings(); renderCameras(); fillZoneCams();
  await loadStatus(); renderCameras();
  document.querySelectorAll('#ds-tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
  $('ds-start').addEventListener('click', () => action('start'));
  $('ds-stop').addEventListener('click', () => action('stop'));
  $('ds-build').addEventListener('click', () => action('build'));
  document.querySelectorAll('#ds-log-kind button').forEach(b => b.addEventListener('click', () => {
    S.logKind = b.dataset.kind; document.querySelectorAll('#ds-log-kind button').forEach(x => x.classList.toggle('active', x === b)); loadLog(); }));
  $('ds-model').addEventListener('change', () => { const m = S.cfg.models.find(x => x.name === $('ds-model').value);
    $('ds-model-help').textContent = m ? `classes: ${m.labels.join(', ')}` : ''; });
  $('ds-settings').addEventListener('submit', async (e) => {
    e.preventDefault(); const f = e.target.elements;
    try { await saveConfig({ model: f.model.value, threshold: Number(f.threshold.value), save_every_s: Number(f.save_every_s.value),
                             frame_skip: Number(f.frame_skip.value), loop_files: f.loop_files.checked, nightly_restart: f.nightly_restart.value }, 'ds-settings-msg'); fillSettings(); loadStatus(); } catch (_) { }
  });
  // cameras
  $('cam-add').addEventListener('click', () => openCam(null));
  $('cam-body').addEventListener('click', (e) => {
    const b = e.target.closest('[data-edit],[data-del],[data-draw]'); if (!b) return;
    if (b.dataset.edit) openCam(Number(b.dataset.edit));
    else if (b.dataset.del) removeCam(Number(b.dataset.del));
    else { S.zcam = S.cfg.streams[Number(b.dataset.draw)].name; fillZoneCams(); showTab('zones'); }
  });
  $('cam-sample').addEventListener('change', (e) => { if (e.target.value) $('cam-form').elements.uri.value = e.target.value; });
  $('cam-test').addEventListener('click', testCam);
  $('cam-form').addEventListener('submit', saveCam);
  $('cam-modal').querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', closeCam));
  // zones
  $('zone-cam').addEventListener('change', (e) => openZones(e.target.value));
  $('zone-refresh').addEventListener('click', () => openZones(S.zcam, true));
  document.querySelectorAll('#zone-tools button').forEach(b => b.addEventListener('click', () => setTool(b.dataset.tool)));
  $('zone-undo').addEventListener('click', undo);
  $('zone-clear').addEventListener('click', () => { if (confirm('Clear every zone of this camera? (Save to keep it that way.)')) { remember(); S.zone = emptyZone(); S.draft = null; changed(); } });
  $('zone-save').addEventListener('click', saveZones);
  $('zone-list').addEventListener('click', (e) => {
    const b = e.target.closest('[data-zdel]'); if (!b) return; remember();
    const [k, i] = b.dataset.zdel.split(':');
    if (k === 'line') S.zone.line = []; else if (k === 'outside') S.zone.outside = null; else S.zone[k].splice(Number(i), 1);
    changed();
  });
  const cv = $('zone-canvas');
  cv.addEventListener('click', onClick);
  cv.addEventListener('dblclick', (e) => { e.preventDefault(); finishDraft(); });
  cv.addEventListener('mousemove', onMove);
  cv.addEventListener('mouseleave', () => { S.hover = null; $('zone-pointer').innerHTML = '&nbsp;'; draw(); });
  document.addEventListener('keydown', (e) => {
    if (S.tab !== 'zones' || e.target.closest('input,select,textarea')) return;
    if (e.key === 'Enter') finishDraft();
    if (e.key === 'Escape') { S.draft = null; draw(); }
    if ((e.ctrlKey || e.metaKey) && e.key === 'z') { e.preventDefault(); undo(); }
  });
  window.addEventListener('resize', () => { sizeCanvas(); draw(); });
  window.addEventListener('beforeunload', (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });
  // timers: status every 3 s, log every 5 s (Pipeline tab), live frames every 2 s (Live tab)
  setInterval(loadStatus, 3000);
  setInterval(() => { if (S.tab === 'pipeline') loadLog(); }, 5000);
  setInterval(refreshLive, 2000);
  const fromHash = () => {                              // #zones=<camera> (links, the Draw button) and back / forward
    const h = location.hash.slice(1).split('=');
    if (h[1]) { S.zcam = decodeURIComponent(h[1]); fillZoneCams(); }
    showTab(['pipeline', 'cameras', 'zones', 'live'].includes(h[0]) ? h[0] : 'pipeline');
  };
  window.addEventListener('hashchange', fromHash);
  fromHash();
});
