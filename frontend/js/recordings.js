/* ────────────────────────────────────────────────────────────────────────────
 * RECORDINGS (Trinetra team): record cameras on this Jetson for a set time → 2 frames (one every N s) → 3 compress + zip
 * → 4 send to the Trinetra laptop (it fetches the parts itself, resumable). backend/captures.py; every step carries on
 * by itself after a dashboard restart, reboot or power cut.
 * ──────────────────────────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
const S = { data: null, open: null, modalFor: null };

/* ─── helpers ─── */
function fmtBytes(b) {
  if (!b) return '0 MB';
  if (b >= 1e9) return `${(b / 1e9).toFixed(b >= 1e10 ? 0 : 1)} GB`;
  return `${(b / 1e6).toFixed(b >= 1e8 ? 0 : 1)} MB`;
}
function fmtMin(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h} h ${pad(m)} min` : m ? `${m} min ${pad(s)} s` : `${s} s`;
}
const fmtDur = (min) => min >= 60 && min % 60 === 0 ? `${min / 60} h` : min >= 60 ? `${Math.floor(min / 60)} h ${min % 60} min` : `${min} min`;
const at = (iso) => iso ? new Date(iso) : null;
const hm = (iso) => { const d = at(iso); return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : ''; };
const dayHm = (iso) => { const d = at(iso); return d ? `${d.getDate()} ${d.toLocaleString('en', { month: 'short' })} · ${hm(iso)}` : ''; };
/** time to send over 4G: measured 2–5 Mbit/s up from a Jio dongle (26 Nov 2026) */
function over4g(bytes) {
  const slow = bytes * 8 / 2e6, fast = bytes * 8 / 5e6;
  const f = (s) => s < 90 ? `${Math.max(1, Math.round(s))} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
  return `${f(fast)}–${f(slow)} over 4G`;
}
function msg(id, text, ok) { const el = $(id); el.className = `form-result ${ok ? 'good' : 'bad'}`; el.textContent = text; }
const pct = (a, b) => b ? Math.min(100, Math.round(100 * a / b)) : 0;
const bar = (p) => `<div class="bar rec-bar"><span class="good" style="width:${p}%"></span></div>`;
const btn = (act, id, label, cls = '') => `<button type="button" class="btn sm ${cls}" data-act="${act}" data-id="${id}">${label}</button>`;

/* ════════ the list ════════ */
async function load() {
  try { S.data = await apiGet('/api/captures'); } catch (e) { return; }
  renderTop(); renderList();
  if (S.open) renderDetail();
}

function renderTop() {
  const d = S.data, k = d.disk, r = d.receiver;
  $('rec-disk').textContent = `${k.free_gb} GB free`;
  $('rcv-folder').textContent = d.folder;
  $('rcv-free').textContent = `${k.free_gb} GB`;
  $('rcv-stop').textContent = `${k.stop_gb} GB free (a new recording needs ${k.start_min_gb} GB)`;
  const dot = $('rcv-dot');
  if (r.age_s == null) { dot.className = 'ds-dot'; $('rcv-state').textContent = 'Not seen yet';
    $('rcv-sub').textContent = 'the laptop has not checked in: set it up once (below), then it checks every minute'; }
  else if (r.age_s < 180) { dot.className = 'ds-dot run'; $('rcv-state').textContent = 'Connected';
    $('rcv-sub').textContent = `${r.host || 'laptop'} checked in ${r.age_s < 60 ? `${r.age_s} s` : `${Math.round(r.age_s / 60)} min`} ago`; }
  else { dot.className = 'ds-dot bad'; $('rcv-state').textContent = 'Not checking in';
    $('rcv-sub').textContent = `${r.host || 'laptop'} last checked in ${dayHm(r.at)} — packages wait here until it is back (laptop on, Tailscale up)`; }
  // cameras (kept ticked across refreshes)
  const box = $('rec-cams'), was = new Set([...box.querySelectorAll('input:checked')].map(i => i.value));
  const names = d.cameras.map(c => c.name).join('|');
  if (box.dataset.names !== names) {
    box.dataset.names = names;
    box.innerHTML = d.cameras.length ? d.cameras.map(c => `<label class="rec-cam" title="${escapeHtml(c.name)}"><input type="checkbox" value="${escapeHtml(c.name)}"${was.has(c.name) ? ' checked' : ''}>
        <span class="rec-cam-text"><span>${escapeHtml(c.name)}</span>${c.enabled ? '' : '<small class="muted">off in the pipeline</small>'}</span></label>`).join('')
      : '<div class="ds-note warn">No cameras yet: add them on the DeepStream page → Cameras (a recording uses the same cameras).</div>';
  }
  estimate();
}

function estimate() {
  const f = $('rec-form').elements, n = $('rec-cams').querySelectorAll('input:checked').length;
  const min = minutesAsked();
  const rec = (S.data?.captures || []).find(c => c.video_bytes && c.cameras.some(x => x.seconds > 30));
  let rate = 2.5e6 / 8;                                                 // bytes/s per camera: a typical 1080p camera ~2.5 Mbit/s
  if (rec) { const s = rec.cameras.reduce((a, c) => a + c.seconds, 0); if (s > 30) rate = rec.video_bytes / s; }
  const bytes = rate * 60 * (min || 0) * n;
  $('rec-estimate').innerHTML = n && min
    ? `≈ <b>${fmtBytes(bytes)}</b> of video on this Jetson for ${n} camera${n > 1 ? 's' : ''} × ${fmtDur(min)} (${rec ? 'at the rate of the last recording' : 'about 2.5 Mbit/s per camera'}) ·
       one frame a second = <b>${(min * 60 * n).toLocaleString()}</b> frames. Nothing leaves the Jetson until you press Send.`
    : 'Choose the cameras and how long to record.';
  $('rec-start').disabled = !n || !min;
}
function minutesAsked() {
  const f = $('rec-form').elements;
  return f.preset.value === 'custom' ? Math.round(Number(f.minutes.value)) : Number(f.preset.value);
}

function videoCell(c) {
  const secs = c.cameras.reduce((a, x) => Math.max(a, x.seconds || 0), 0);
  const size = `${fmtBytes(c.video_bytes)} · ${fmtMin(secs)}`;
  if (c.state === 'scheduled') return `<span class="tag warn">Starts ${dayHm(c.start)}</span><div class="who-sub">${fmtDur(c.minutes)}</div>${btn('stop', c.id, 'Cancel', 'ghost')}`;
  if (c.state === 'recording') {
    const left = (at(c.end) - Date.now()) / 1000, total = c.minutes * 60;
    const live = c.cameras.filter(x => x.state === 'recording').length;
    return `<span class="tag on_desk"><span class="rec-live"></span>Recording</span>
      <div class="who-sub">${live} of ${c.cameras.length} camera${c.cameras.length > 1 ? 's' : ''} live · ${fmtMin(left)} left</div>
      ${bar(pct(total - left, total))}<div class="who-sub">${size}</div>${btn('stop', c.id, '■ Stop', 'danger-ghost')}`;
  }
  if (c.state === 'recorded' || c.state === 'stopped')
    return `<span class="tag ${c.video_bytes ? 'dept' : 'bad'}">${c.video_bytes ? (c.state === 'stopped' ? 'Stopped early' : 'Recorded') : 'No video'}</span>
      <div class="who-sub">${size}</div>${c.note ? `<div class="who-sub warn-ink">${escapeHtml(c.note)}</div>` : ''}`;
  return `<span class="tag bad">${c.state === 'cancelled' ? 'Cancelled' : 'Failed'}</span>${c.note ? `<div class="who-sub">${escapeHtml(c.note)}</div>` : ''}`;
}

function framesCell(c) {
  const f = c.frames, done = (c.state === 'recorded' || c.state === 'stopped') && c.video_bytes;
  if (!f) return done ? btn('frames', c.id, 'Make frames', 'primary') : '<span class="muted">after the recording</span>';
  if (f.state === 'running') return `<span class="tag warn">Making frames…</span>${bar(pct(f.done, f.total))}
      <div class="who-sub">${f.count.toLocaleString()} so far · piece ${f.done} of ${f.total}${f.attempts > 1 ? ` · try ${f.attempts}` : ''}</div>`;
  if (f.state === 'failed') return `<span class="tag bad">Failed</span><div class="who-sub">${escapeHtml(f.error || '')}</div>${btn('frames', c.id, 'Try again')}`;
  return `<span class="tag dept">${f.count.toLocaleString()} frames</span>
    <div class="who-sub">one every ${f.every_s} s${f.skip_similar ? ` · ${f.skipped.toLocaleString()} similar skipped` : ''} · ${fmtBytes(f.bytes)}</div>
    ${btn('frames', c.id, 'Again…', 'ghost')}`;
}

function packCell(c) {
  const p = c.pack, f = c.frames;
  if (!p) return f && f.state === 'done' ? (f.count ? btn('pack', c.id, 'Compress + zip', 'primary') : '<span class="muted">no frames</span>') : '<span class="muted">after the frames</span>';
  if (p.state === 'running') return `<span class="tag warn">Packing…</span>${bar(pct(p.frames_done, p.total))}
      <div class="who-sub">${p.frames_done.toLocaleString()} of ${p.total.toLocaleString()} frames · ${p.parts} part${p.parts === 1 ? '' : 's'} ready</div>`;
  if (p.state === 'failed') return `<span class="tag bad">Failed</span><div class="who-sub">${escapeHtml(p.error || '')}</div>${btn('pack', c.id, 'Try again')}`;
  return `<span class="tag dept"><span data-icon="package"></span>${fmtBytes(p.bytes)}</span>
    <div class="who-sub">${p.parts} part${p.parts === 1 ? '' : 's'} · ${p.max_px ? `${p.max_px} px` : 'as recorded'} · q${p.quality}</div>
    <div class="who-sub">${over4g(p.bytes)}</div>${btn('pack', c.id, 'Again…', 'ghost')}`;
}

function sendCell(c) {
  const s = c.send, p = c.pack;
  if (!p || p.state !== 'done') return '<span class="muted">after the package</span>';
  if (s.state === 'none') return btn('send', c.id, '<span data-icon="upload"></span>Send to laptop', 'primary');
  if (s.state === 'waiting') return `<span class="tag warn">Waiting for the laptop</span><div class="who-sub">asked ${hm(s.asked)}</div>${btn('cancel-send', c.id, 'Cancel', 'ghost')}`;
  if (s.state === 'sending') return `<span class="tag warn">Sending…</span>${bar(pct(s.bytes_done, s.bytes))}
      <div class="who-sub">part ${s.parts_done} of ${s.parts} · ${fmtBytes(s.bytes_done)} of ${fmtBytes(s.bytes)}</div>
      <div class="who-sub">${s.host ? escapeHtml(s.host) + ' · ' : ''}last ${hm(s.at)}</div>${btn('cancel-send', c.id, 'Cancel', 'ghost')}`;
  return `<span class="tag on_desk">Received ✓</span><div class="who-sub">${escapeHtml(s.host || 'laptop')} · ${dayHm(s.at)}</div>
    ${btn('send', c.id, 'Send again', 'ghost')}`;
}

function renderList() {
  const list = S.data.captures;
  $('rec-count').textContent = list.length ? `${list.length} on this Jetson · ${fmtBytes(list.reduce((a, c) => a + c.video_bytes + (c.frames?.bytes || 0) + (c.pack?.bytes || 0), 0))}` : '';
  $('rec-body').innerHTML = list.length ? list.map(c => `<tr class="${S.open === c.id ? 'rec-open' : ''}">
      <td><div class="who-name">${escapeHtml(c.name || c.id)}</div>
        <div class="who-sub">${c.cameras.map(x => escapeHtml(x.name)).join(', ')}</div>
        <div class="who-sub">${dayHm(c.start)} · ${fmtDur(c.minutes)}${c.by ? ` · ${escapeHtml(c.by)}` : ''}</div>
        <div class="rec-row-acts">${btn('open', c.id, S.open === c.id ? 'Hide details' : 'Details', 'ghost')}
          ${c.state === 'recording' ? '' : btn('delete', c.id, '<span data-icon="trash"></span>Delete', 'ghost danger-ghost')}</div></td>
      <td class="rec-cell">${videoCell(c)}</td><td class="rec-cell">${framesCell(c)}</td>
      <td class="rec-cell">${packCell(c)}</td><td class="rec-cell">${sendCell(c)}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">No recordings yet: choose cameras above and press Start recording.</td></tr>';
  hydrateIcons($('rec-body'));
}

/* ════════ one recording ════════ */
async function renderDetail() {
  const c = S.data.captures.find(x => x.id === S.open);
  if (!c) { S.open = null; $('rec-detail').hidden = true; return; }
  $('rec-detail').hidden = false;
  $('det-title').textContent = c.name || `Recording ${c.id}`;
  $('det-hint').textContent = `${c.id} · ${dayHm(c.start)} → ${hm(c.ended || c.end)}`;
  $('det-cams').innerHTML = c.cameras.map(x => `<tr>
      <td class="who-name">${escapeHtml(x.name)}<div class="who-sub">${escapeHtml(x.codec || '')}</div></td>
      <td>${{ recording: '<span class="tag on_desk">Recording</span>', connecting: '<span class="tag warn">Connecting…</span>',
              retrying: '<span class="tag bad">Camera lost — retrying</span>', done: '<span class="tag dept">Done</span>',
              interrupted: '<span class="tag warn">Carrying on…</span>', failed: '<span class="tag bad">Failed</span>',
              waiting: '<span class="tag">Waiting</span>' }[x.state] || `<span class="tag">${escapeHtml(x.state)}</span>`}
          ${x.stop_reason ? `<div class="who-sub">${escapeHtml(x.stop_reason)}</div>` : ''}</td>
      <td class="mono">${fmtBytes(x.bytes)} · ${fmtMin(x.seconds)}</td><td class="mono">${x.reconnects}</td>
      <td class="who-sub">${escapeHtml(x.error || '—')}</td>
      <td>${c.frames && c.frames.count ? `<a href="/api/captures/${c.id}/frame/${encodeURIComponent(x.name)}" target="_blank" rel="noopener">view</a>` : '<span class="muted">—</span>'}</td></tr>`).join('');
  try {
    const r = await apiGet(`/api/captures/${c.id}/log`);
    const pre = $('det-log'), atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = r.text; if (atEnd) pre.scrollTop = pre.scrollHeight;
  } catch (_) { /* gone meanwhile */ }
}

/* ════════ actions ════════ */
async function act(a, id) {
  const c = S.data.captures.find(x => x.id === id); if (!c) return;
  const label = c.name || id;
  try {
    if (a === 'open') { S.open = S.open === id ? null : id; renderList(); if (S.open) { await renderDetail(); $('rec-detail').scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } else $('rec-detail').hidden = true; return; }
    if (a === 'stop') {
      if (!confirm(c.state === 'scheduled' ? `Cancel the planned recording “${label}”?` : `Stop recording “${label}” now? What is recorded so far is kept.`)) return;
      await apiPost(`/api/captures/${id}/stop`); toast(c.state === 'scheduled' ? 'Cancelled' : 'Stopping — the last piece is closed cleanly');
    } else if (a === 'frames') return openFrames(c);
    else if (a === 'pack') return openPack(c);
    else if (a === 'send') {
      await apiPost(`/api/captures/${id}/send`);
      toast(S.data.receiver.age_s != null && S.data.receiver.age_s < 180 ? 'In line: the laptop fetches it within a minute' : 'In line: it is sent when the laptop checks in');
    } else if (a === 'cancel-send') {
      if (!confirm(`Stop sending “${label}” to the laptop? Parts already on the laptop stay there; Send again continues.`)) return;
      await apiPost(`/api/captures/${id}/cancel-send`); toast('Sending cancelled');
    } else if (a === 'delete') {
      const size = c.video_bytes + (c.frames?.bytes || 0) + (c.pack?.bytes || 0);
      const warn = c.pack && c.send.state !== 'received' ? '\n\nIt has NOT been received by the laptop.' : '';
      if (!confirm(`Delete “${label}” from this Jetson: video, frames and package (${fmtBytes(size)})? This cannot be undone.${warn}`)) return;
      await apiDelete(`/api/captures/${id}`); if (S.open === id) { S.open = null; $('rec-detail').hidden = true; } toast('Deleted');
    }
  } catch (e) { toast(e.message); }
  load();
}

function openModal(id, c) { S.modalFor = c.id; $(id).hidden = false; }
function closeModals() { ['frames-modal', 'pack-modal'].forEach(id => { $(id).hidden = true; }); S.modalFor = null; }

function openFrames(c) {
  const f = $('frames-form').elements;
  f.every_s.innerHTML = S.data.frame_every.map(n => `<option value="${n}"${n === (c.frames?.every_s || 1) ? ' selected' : ''}>${n} s</option>`).join('');
  f.skip_similar.checked = !!c.frames?.skip_similar;
  $('frames-msg').textContent = '';
  const upd = () => {
    const secs = c.cameras.reduce((a, x) => a + (x.seconds || 0), 0), n = Math.round(secs / Number(f.every_s.value));
    $('frames-est').textContent = `≈ ${n.toLocaleString()} frames${f.skip_similar.checked ? ' or fewer' : ''} from ${fmtMin(secs)} of video (all cameras), at full size, on this Jetson.` +
      (c.frames ? ' The frames and package made before are replaced.' : '');
  };
  f.every_s.onchange = upd; f.skip_similar.onchange = upd; upd();
  openModal('frames-modal', c);
}

function openPack(c) {
  const f = $('pack-form').elements;
  $('pack-msg').textContent = '';
  const upd = () => {
    const per = { 0: 380e3, 1920: 380e3, 1280: 150e3, 960: 90e3, 640: 45e3 }[f.max_px.value] * (Number(f.quality.value) / 85);
    const b = per * (c.frames?.count || 0);
    $('pack-est').innerHTML = `≈ <b>${fmtBytes(b)}</b> for ${(c.frames?.count || 0).toLocaleString()} frames (${over4g(b)}). JPEGs are compressed
      already, so zip only bundles them; a smaller frame size is what makes the package small. Training uses 640–1280 px.`;
  };
  f.max_px.onchange = upd; f.quality.oninput = upd; upd();
  openModal('pack-modal', c);
}

onReady(() => {
  const f = $('rec-form').elements;
  f.preset.addEventListener('change', () => { $('rec-minutes-wrap').hidden = f.preset.value !== 'custom'; estimate(); });
  f.minutes.addEventListener('input', estimate);
  f.when.addEventListener('change', () => {
    $('rec-at-wrap').hidden = f.when.value !== 'at';
    if (f.when.value === 'at' && !f.start_at.value) {
      const d = new Date(Date.now() + 3600e3); d.setMinutes(0, 0, 0);
      f.start_at.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:00`;
    }
  });
  $('rec-cams').addEventListener('change', estimate);
  $('rec-all').addEventListener('click', () => { const boxes = [...$('rec-cams').querySelectorAll('input')];
    const all = boxes.every(b => b.checked); boxes.forEach(b => { b.checked = !all; }); estimate(); });
  $('rec-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const cams = [...$('rec-cams').querySelectorAll('input:checked')].map(i => i.value), min = minutesAsked();
    if (!cams.length) return msg('rec-msg', 'Choose at least one camera');
    if (!(min >= 1 && min <= S.data.max_minutes)) return msg('rec-msg', `Duration: 1 to ${S.data.max_minutes} minutes`);
    const body = { cameras: cams, minutes: min, name: f.name.value.trim() };
    if (f.when.value === 'at') { if (!f.start_at.value) return msg('rec-msg', 'Choose the start time'); body.start_at = f.start_at.value; }
    $('rec-start').disabled = true;
    try {
      const c = await apiPost('/api/captures', body);
      msg('rec-msg', c.state === 'scheduled' ? `Planned: starts ${dayHm(c.start)}` : `Recording ${cams.length} camera${cams.length > 1 ? 's' : ''} until ${hm(c.end)}`, true);
      f.name.value = '';
    } catch (err) { msg('rec-msg', err.message); }
    $('rec-start').disabled = false; load();
  });
  $('rec-body').addEventListener('click', (e) => { const b = e.target.closest('[data-act]'); if (b) act(b.dataset.act, b.dataset.id); });
  $('det-close').addEventListener('click', () => { S.open = null; $('rec-detail').hidden = true; renderList(); });
  document.querySelectorAll('.modal-backdrop [data-close]').forEach(b => b.addEventListener('click', closeModals));
  $('frames-form').addEventListener('submit', async (e) => {
    e.preventDefault(); const f2 = e.target.elements;
    try { await apiPost(`/api/captures/${S.modalFor}/frames`, { every_s: Number(f2.every_s.value), skip_similar: f2.skip_similar.checked });
          closeModals(); toast('Making frames — it carries on even if this page is closed'); load(); }
    catch (err) { msg('frames-msg', err.message); }
  });
  $('pack-form').addEventListener('submit', async (e) => {
    e.preventDefault(); const f2 = e.target.elements;
    try { await apiPost(`/api/captures/${S.modalFor}/pack`, { max_px: Number(f2.max_px.value), quality: Number(f2.quality.value), part_mb: Number(f2.part_mb.value) });
          closeModals(); toast('Packing — it carries on even if this page is closed'); load(); }
    catch (err) { msg('pack-msg', err.message); }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModals(); });
  load();
  setInterval(() => { if (!document.hidden) load(); }, 3000);
});
