/* ────────────────────────────────────────────────────────────────────────────
 * DEEPSTREAM — the perimeter pipeline (Trinetra team): Pipeline (start / stop / engine / settings / log),
 * Cameras (RTSP or a test video, connection test), Perimeter zones (drawn on the camera's picture) and Live view.
 * Zones are fractions of the frame; the outside-side rule below is the same as backend/pipeline/zones.py.
 * ──────────────────────────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
const S = { tab: 'pipeline', cfg: null, status: null, logKind: 'pipeline', editing: null, zcam: null };

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
  if ((st.throw_no_band || []).length) { note.className = 'ds-note warn'; note.textContent += ` Throw detection is on for ${st.throw_no_band.join(', ')} but no throw band is drawn (Perimeter zones tab → Throw band).`; }
  const rows = st.state.streams || [];
  $('ds-streams').innerHTML = rows.length ? rows.map(s => `<tr>
      <td class="who-name">${escapeHtml(s.name)}<div class="who-sub">${escapeHtml(s.type)}${s.reconnects ? ` · ${s.reconnects} reconnects` : ''}</div></td>
      <td>${s.online ? '<span class="tag on_desk">Online</span>' : `<span class="tag bad">Offline</span>${s.reachable === false ? ' <span class="muted">not reachable</span>' : ''}`}</td>
      <td class="mono">${s.fps ?? 0}${s.throw ? `<div class="who-sub">throw: every frame${s.online && s.fps && s.fps < 20 ? ' · <span style="color:var(--red)">low fps</span>' : ''}</div>` : ''}</td>
      <td>${s.mode ? `<span class="tag ${s.mode === 'night' ? 'dept' : 'warn'}">${s.mode === 'night' ? 'Night' : 'Day'}</span><div class="who-sub">colour ${s.saturation ?? '?'}</div>` : '<span class="muted">—</span>'}</td>
      <td>${Object.entries(s.images || {}).map(([c, n]) => `<span class="tag">${escapeHtml(c)} ${n}</span>`).join('') || '<span class="muted">none</span>'}</td></tr>`).join('')
    : `<tr><td colspan="5" class="empty">${st.running ? 'starting…' : 'not running'}</td></tr>`;
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
  f.night_saturation_max.value = c.night_saturation_max ?? 12; f.mode_hysteresis_s.value = c.mode_hysteresis_s ?? 30;
  renderClasses(m, c.classes || []);
}
function renderClasses(m, keep) {
  const labels = m ? m.labels : [];
  $('ds-classes').innerHTML = labels.map(l => `<label class="rec-cam"><input type="checkbox" value="${escapeHtml(l)}"${keep.includes(l) ? ' checked' : ''}>
    <span class="rec-cam-text"><span>${escapeHtml(l)}</span></span></label>`).join('') || '<span class="muted">no model</span>';
  $('ds-classes-hint').textContent = labels.length ? (keep.length ? `${keep.length} of ${labels.length}` : `all ${labels.length}`) : '';
}
const keptClasses = () => [...$('ds-classes').querySelectorAll('input:checked')].map(i => i.value);

async function saveConfig(patch, msgId) {
  try {
    const out = await apiPut('/api/ds/config', { ...S.cfg, ...patch });
    S.cfg = out;
    if (msgId) msg(msgId, out.restart_needed ? 'Saved — restart the pipeline to use it.' : 'Saved.', true);
    return out;
  } catch (e) { if (msgId) msg(msgId, e.message, false); throw e; }
}

/* ════════ cameras ════════ */
const camPager = pager('cam-pager', () => renderCameras());     // the Workplace Analytics pager

function renderCameras() {
  const st = S.cfg.streams, drawn = S.status?.zones_drawn || [];
  $('cam-count').textContent = st.length;
  const rows = camPager.slice(st.map((s, i) => [s, i]));          // the index in the full list: Edit / Remove / Draw
  $('cam-body').innerHTML = st.length ? rows.map(([s, i]) => `<tr class="${s.enabled ? '' : 'off'}">
      <td class="who-name">${escapeHtml(s.name)}</td>
      <td class="url-cell" title="${escapeHtml(maskUri(s.uri))}">${escapeHtml(maskUri(s.uri))}</td>
      <td class="mono">${s.uri.startsWith('file://') ? 'file' : s.transport.toUpperCase()}</td>
      <td>${s.enabled ? '<span class="tag on_desk">On</span>' : '<span class="tag">Off</span>'}</td>
      <td>${s.throw ? `<span class="tag warn">Every frame</span>${(S.status?.throw_no_band || []).includes(s.name) ? '<div class="who-sub" style="color:var(--red)">no throw band drawn</div>' : ''}` : '<span class="muted">off</span>'}</td>
      <td>${drawn.includes(s.name) ? '<span class="tag on_desk">Drawn</span>' : '<span class="tag bad">Not drawn</span>'}</td>
      <td class="row-actions"><button class="btn sm" data-draw="${i}">Draw zones</button>
        <button class="btn sm" data-edit="${i}">Edit</button><button class="btn sm danger-ghost" data-del="${i}">Remove</button></td></tr>`).join('')
    : '<tr><td colspan="7" class="empty">No cameras yet — “Add camera”.</td></tr>';
}

function openCam(i) {
  S.editing = i;
  const f = $('cam-form').elements, s = i == null ? null : S.cfg.streams[i];
  $('cam-form').reset(); $('cam-msg').textContent = '';
  $('cam-title').textContent = s ? `Edit ${s.name}` : 'Add camera';
  f.name.value = s?.name || ''; f.uri.value = s?.uri || ''; f.transport.value = s?.transport || 'tcp'; f.enabled.checked = s ? s.enabled : true;
  f.throw.checked = !!s?.throw;
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
    if (r.ok && f.throw.checked && r.fps && r.fps < 25)
      msg('cam-msg', `Connected: ${r.width}×${r.height} ${r.codec} · ${r.fps} fps — throw detection needs 25 fps or more: raise the camera's frame rate (its web page → Video)`, false);
    else if (r.ok) msg('cam-msg', `Connected: ${r.width}×${r.height} ${r.codec}${r.fps ? ` · ${r.fps} fps` : ''}`, true);
    else msg('cam-msg', `No video: ${r.error}`, false);
  } catch (e) { msg('cam-msg', e.message, false); }
  finally { $('cam-test').disabled = false; }
}

async function saveCam(ev) {
  ev.preventDefault();
  const f = $('cam-form').elements;
  const cam = { name: f.name.value.trim(), uri: f.uri.value.trim(), transport: f.transport.value, enabled: f.enabled.checked, throw: f.throw.checked };
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

/* ════════ perimeter zones (js/zone-editor.js) ════════ */
let ZE = null;
function fillZoneCams() { if (ZE) ZE.setCameras(S.cfg.streams.map(s => s.name), S.zcam); }
function openZones(cam) { if (!ZE) return; ZE.setCameras(S.cfg.streams.map(s => s.name), cam || S.zcam); ZE.open(ZE.camera); S.zcam = ZE.camera; }

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
    $('ds-model-help').textContent = m ? `classes: ${m.labels.join(', ')}` : ''; renderClasses(m, []); });
  $('ds-classes').addEventListener('change', () => { const m = S.cfg.models.find(x => x.name === $('ds-model').value);
    const k = keptClasses(); $('ds-classes-hint').textContent = m ? (k.length ? `${k.length} of ${m.labels.length}` : `all ${m.labels.length}`) : ''; });
  $('ds-settings').addEventListener('submit', async (e) => {
    e.preventDefault(); const f = e.target.elements;
    try { await saveConfig({ model: f.model.value, threshold: Number(f.threshold.value), save_every_s: Number(f.save_every_s.value),
                             frame_skip: Number(f.frame_skip.value), loop_files: f.loop_files.checked, nightly_restart: f.nightly_restart.value,
                             classes: keptClasses(), night_saturation_max: Number(f.night_saturation_max.value),
                             mode_hysteresis_s: Number(f.mode_hysteresis_s.value) }, 'ds-settings-msg'); fillSettings(); loadStatus(); } catch (_) { }
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
  // zones: the shared editor (team: the DeepStream zone API)
  ZE = ZoneEditor(document.getElementById('zone-editor'), {
    team: true,                                         // the Trinetra team: near-wall, throw band, landing zone too
    zoneUrl: (cam) => `/api/ds/zones/${encodeURIComponent(cam)}`,
    frameUrl: (cam, refresh) => `/api/ds/frame/${encodeURIComponent(cam)}?refresh=${refresh ? 1 : 0}`,
    onSaved: () => loadStatus(),
  });
  fillZoneCams();
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
