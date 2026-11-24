/* ────────────────────────────────────────────────────────────────────────────
 * ALERTS — the alert history (filters, server pages, acknowledge), the alert rules the client sets up (presence,
 * dwell, count, offline; region, who, cameras, days / hours, gap, repeat, cooldown, severity, sound) and the regions
 * (the shared zone editor; the client's admins).
 * ──────────────────────────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
const S = { tab: 'history', range: null, sev: '', status: '', camera: '', q: '', rules: [], cams: {}, special: {},
            editing: null, seq: 0 };
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const TYPE = { presence: 'In the region', dwell: 'Stays in the region', count: 'Too many in the region', offline: 'Camera offline' };
const WHO = { person: 'a person', object: 'an object', any: 'a person or an object' };
const alPager = pager('al-pager', () => loadHistory());
const rulePager = pager('rule-pager', () => renderRules());
let ZE = null;

function fmtTs(iso) {
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  return `${fmtDay(d)} · ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
const sevTag = (s) => s === 'critical' ? '<span class="tag bad">Critical</span>' : '<span class="tag warn">Warning</span>';

/* ════════ tabs ════════ */
function showTab(t) {
  if (['regions', 'speaker'].includes(t) && !can('alert_rules')) t = 'history';
  S.tab = t;
  document.querySelectorAll('#al-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  ['history', 'rules', 'regions', 'speaker'].forEach(x => { $(`tab-${x}`).hidden = x !== t; });
  document.querySelectorAll('#date-pill, #range-seg').forEach(e => { e.style.visibility = t === 'history' ? '' : 'hidden'; });
  const s = document.querySelector('.page-head .search'); if (s) s.hidden = t !== 'history';
  history.replaceState(null, '', `${location.search}#${t}`);
  if (t === 'rules') loadRules();
  if (t === 'regions') openRegions();
  clearInterval(SPK.timer);
  if (t === 'speaker') { loadSpeaker(); SPK.timer = setInterval(loadSpeaker, 3000); }
}

/* ════════ the alerts ════════ */
async function loadHistory() {
  if (!S.range) return;
  const seq = ++S.seq;
  const q = new URLSearchParams({ date_from: S.range.from, date_to: S.range.to, page: alPager.page, size: alPager.size });
  if (S.sev) q.set('severity', S.sev);
  if (S.status) q.set('status', S.status);
  if (S.camera) q.set('camera', S.camera);
  if (S.q) q.set('q', S.q);
  let d;
  try { d = await apiGet(`/api/alerts?${q}`); } catch (e) { toast(`Could not load the alerts: ${e.message}`); return; }
  if (seq !== S.seq) return;
  $('al-count').textContent = d.total;
  alPager.total(d.total);
  const sel = $('cam-filter'), cur = sel.value;
  sel.innerHTML = '<option value="">All cameras</option>' + d.cameras.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  sel.value = d.cameras.includes(cur) ? cur : '';
  const ack = can('manage_alerts');
  $('al-body').innerHTML = d.items.length ? d.items.map(a => `<tr class="${a.acked ? '' : (a.severity === 'critical' ? 'row-crit' : 'row-warn')}">
      <td class="nowrap">${fmtTs(a.triggered_at)}</td>
      <td><div class="who-name">${escapeHtml(a.rule_name)}</div>${sevTag(a.severity)}</td>
      <td>${escapeHtml(a.message)}${a.badge && a.kind !== 'offline' ? `<div class="emp-cell-sub">${escapeHtml(a.badge)}</div>` : ''}</td>
      <td>${escapeHtml(a.camera)}${a.zone ? `<div class="emp-cell-sub">${escapeHtml(a.zone)}</div>` : ''}</td>
      <td>${a.has_image ? `<button class="snap-thumb" data-snap="${a.id}" title="Open the snapshot"><img src="/images/${encodeURIComponent(a.image)}" alt="" loading="lazy"></button>` : '<span class="muted">—</span>'}</td>
      <td class="nowrap">${a.acked ? `<span class="tag on_desk">Acknowledged</span><div class="emp-cell-sub">${escapeHtml(a.acked_by || '')}</div>`
        : (ack ? `<button class="btn sm" data-ack="${a.id}"><span class="ic-inline">✓</span>Acknowledge</button>` : '<span class="tag warn">Open</span>')}</td>
    </tr>`).join('') : `<tr><td colspan="6" class="empty">No alerts ${S.q || S.sev || S.status || S.camera ? 'match' : 'in this period'}.</td></tr>`;
  S.items = d.items;
}

async function ack(id) {
  try { await apiPost(`/api/alerts/${id}/ack`, {}); toast('Acknowledged'); loadHistory(); refreshAlertStrip(); }
  catch (e) { toast(e.message); }
}
async function ackAll() {
  if (!confirm('Acknowledge every open alert?')) return;
  try { const r = await apiPost('/api/alerts/ack-all', {}); toast(`${r.acknowledged} alert(s) acknowledged`); loadHistory(); refreshAlertStrip(); }
  catch (e) { toast(e.message); }
}
function openSnap(id) {
  const a = (S.items || []).find(x => x.id === Number(id)); if (!a) return;
  $('snap-title').textContent = a.rule_name;
  $('snap-sub').textContent = `${a.message} · ${a.camera}${a.zone ? ' · ' + a.zone : ''} · ${fmtTs(a.triggered_at)}`;
  $('snap-img').src = `/images/${encodeURIComponent(a.image)}`;
  $('snap-modal').hidden = false;
}

/* ════════ rules ════════ */
async function loadRules() {
  try {
    const d = await apiGet('/api/alert-rules');
    S.rules = d.rules; S.cams = d.cameras; S.special = d.special_regions;
  } catch (e) { toast(`Could not load the rules: ${e.message}`); return; }
  renderRules();
}
function regionKnown(r) {
  if (r.type === 'offline' || S.special[r.region]) return true;
  const cams = r.cameras.length ? r.cameras : Object.keys(S.cams);
  return cams.some(c => (S.cams[c] || []).some(x => x.name.toLowerCase() === r.region.toLowerCase()));
}
function whenText(r) {
  const who = WHO[r.target];
  return r.type === 'presence' ? `${who} is in the region`
    : r.type === 'dwell' ? `${who} stays ${r.minutes} min`
    : r.type === 'count' ? `${r.count}+ ${r.target === 'person' ? 'people' : r.target === 'object' ? 'objects' : 'people / objects'} at once`
    : `no video for ${r.minutes} min`;
}
function activeText(r) {
  const days = r.days.length === 7 ? 'every day' : r.days.length === 5 && r.days.every(d => d < 5) ? 'Mon–Fri' : r.days.map(d => DAYS[d]).join(', ');
  return `${days} · ${r.start ? `${r.start}–${r.end}` : 'all day'}`;
}
function renderRules() {
  const edit = can('alert_rules');
  $('rule-count').textContent = S.rules.length;
  const rows = rulePager.slice(S.rules);
  $('rule-body').innerHTML = rows.length ? rows.map(r => `<tr class="${r.enabled ? '' : 'disabled-row'}">
      <td><label class="as-switch" title="${r.enabled ? 'On' : 'Off'}"><input type="checkbox" data-toggle="${r.id}" ${r.enabled ? 'checked' : ''} ${edit ? '' : 'disabled'}><span class="as-track"></span></label></td>
      <td><div class="who-name">${escapeHtml(r.name)}</div><div class="emp-cell-sub">${TYPE[r.type]}${r.sound ? ' · sound' : ''}</div></td>
      <td>${escapeHtml(whenText(r))}${['presence', 'dwell'].includes(r.type) ? `<div class="emp-cell-sub">gone after ${r.gap} s unseen${r.repeat ? ` · repeats every ${r.repeat} min` : ''}</div>` : ''}</td>
      <td>${r.type === 'offline' ? '—' : escapeHtml(r.region_label)}${regionKnown(r) ? '' : ' <span class="tag bad" title="No camera has a region with this name">no such region</span>'}
        <div class="emp-cell-sub">${r.cameras.length ? escapeHtml(r.cameras.join(', ')) : 'all cameras'}</div></td>
      <td class="nowrap">${escapeHtml(activeText(r))}${r.cooldown ? `<div class="emp-cell-sub">quiet ${r.cooldown} min after an alert</div>` : ''}</td>
      <td>${sevTag(r.severity)}</td>
      <td class="nowrap">${r.last_alert ? fmtTs(r.last_alert) : '<span class="muted">never</span>'}</td>
      <td class="row-actions">${edit ? `<button class="btn sm" data-edit="${r.id}">${icon('pencil')}Edit</button>
        <button class="icon-btn sm" data-del="${r.id}" aria-label="Delete rule" title="Delete rule">${icon('trash')}</button>` : ''}</td>
    </tr>`).join('') : `<tr><td colspan="8" class="empty">No alert rules — nothing raises an alert. ${edit ? 'Create one with “New rule”.' : ''}</td></tr>`;
}

function regionOptions(sel) {
  const names = new Map();
  Object.values(S.cams).forEach(rs => rs.forEach(r => names.set(r.name.toLowerCase(), r)));
  return Object.entries(S.special).map(([k, v]) => `<option value="${k}">${escapeHtml(v[0].toUpperCase() + v.slice(1))}</option>`).join('')
    + [...names.values()].map(r => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)} (${r.kind} region)</option>`).join('')
    + (sel && !S.special[sel] && !names.has(sel.toLowerCase()) ? `<option value="${escapeHtml(sel)}">${escapeHtml(sel)} (no such region now)</option>` : '');
}
function openRule(r) {
  S.editing = r || null;
  const f = $('rule-form').elements, d = r || { name: '', type: 'dwell', target: 'person', region: '*outside', minutes: 10, count: 3,
    cameras: [], days: [0, 1, 2, 3, 4, 5, 6], start: '', end: '', gap: 60, repeat: 0, cooldown: 0, severity: 'warning', sound: true, enabled: true };
  $('rule-title').textContent = r ? `Edit “${r.name}”` : 'New alert rule';
  f.name.value = d.name; f.type.value = d.type; f.target.value = d.target;
  f.region.innerHTML = regionOptions(d.region); f.region.value = d.region || '*alert';
  f.minutes.value = d.minutes || 5; f.count.value = d.count || 3; f.start.value = d.start; f.end.value = d.end;
  f.gap.value = d.gap ?? 60; f.repeat.value = d.repeat || 0; f.cooldown.value = d.cooldown || 0;
  f.sound.checked = d.sound; f.enabled.checked = d.enabled;
  $('rule-cams').innerHTML = `<label class="radio"><input type="checkbox" value="" ${d.cameras.length ? '' : 'checked'} data-allcams> All cameras</label>`
    + Object.keys(S.cams).map(c => `<label class="radio"><input type="checkbox" value="${escapeHtml(c)}" ${d.cameras.includes(c) ? 'checked' : ''}> ${escapeHtml(c)}</label>`).join('');
  $('rule-days').innerHTML = DAYS.map((n, i) => `<label class="day"><input type="checkbox" value="${i}" ${d.days.includes(i) ? 'checked' : ''}><span>${n}</span></label>`).join('');
  document.querySelectorAll('#rule-sev button').forEach(b => b.classList.toggle('active', b.dataset.sev === d.severity));
  $('rule-result').textContent = '';
  syncForm();
  $('rule-modal').hidden = false; f.name.focus();
}
function closeRule() { $('rule-modal').hidden = true; S.editing = null; }
function ruleBody() {
  const f = $('rule-form').elements;
  const cams = [...$('rule-cams').querySelectorAll('input:checked')].map(x => x.value).filter(Boolean);
  return { name: f.name.value.trim(), type: f.type.value, target: f.target.value, region: f.region.value,
           minutes: Number(f.minutes.value), count: Number(f.count.value), cameras: $('rule-cams').querySelector('[data-allcams]').checked ? [] : cams,
           days: [...$('rule-days').querySelectorAll('input:checked')].map(x => Number(x.value)), start: f.start.value, end: f.end.value,
           gap: Number(f.gap.value), repeat: Number(f.repeat.value), cooldown: Number(f.cooldown.value),
           severity: document.querySelector('#rule-sev button.active')?.dataset.sev || 'warning', sound: f.sound.checked, enabled: f.enabled.checked };
}
function syncForm() {
  const t = $('rule-form').elements.type.value;
  document.querySelectorAll('#rule-form [data-for]').forEach(e => { e.hidden = !e.dataset.for.split(' ').includes(t); });
  document.querySelectorAll('#rule-form [data-show]').forEach(e => { e.hidden = e.dataset.show !== t; });
  const b = ruleBody(), reg = S.special[b.region] || `region “${b.region}”`;
  const cams = b.cameras.length ? `on ${b.cameras.join(', ')}` : 'on any camera';
  const days = b.days.length === 7 ? 'every day' : b.days.map(d => DAYS[d]).join(', ');
  const hours = b.start && b.end ? `${b.start}–${b.end}` : 'all day';
  const what = b.type === 'presence' ? `${WHO[b.target]} is in ${reg}`
    : b.type === 'dwell' ? `${WHO[b.target]} stays in ${reg} for ${b.minutes} min (short gaps up to ${b.gap} s in the tracking do not restart the count)`
    : b.type === 'count' ? `${b.count} or more (${WHO[b.target].replace('a ', '').replace('an ', '')}) are in ${reg} at once`
    : `a camera has no video for ${b.minutes} min`;
  $('rule-preview').innerHTML = `<b>${b.severity === 'critical' ? 'Critical' : 'Warning'} alert</b> when ${escapeHtml(what)}, ${escapeHtml(cams)}, ${escapeHtml(days)}, ${escapeHtml(hours)}.`
    + (['presence', 'dwell'].includes(b.type) ? ` ${b.repeat ? `Repeats every ${b.repeat} min while it lasts.` : 'Once per presence.'}` : '')
    + (b.cooldown ? ` Then quiet for ${b.cooldown} min.` : '');
}
async function saveRule(ev) {
  ev.preventDefault();
  const res = $('rule-result'); res.className = 'form-result bad';
  $('rule-save').disabled = true;
  try {
    const b = ruleBody();
    if (S.editing) await apiPut(`/api/alert-rules/${S.editing.id}`, b); else await apiPost('/api/alert-rules', b);
    closeRule(); toast(`Rule “${b.name}” saved`); loadRules();
  } catch (e) { res.textContent = e.message; }
  finally { $('rule-save').disabled = false; }
}
async function toggleRule(id, on) {
  const r = S.rules.find(x => x.id === Number(id)); if (!r) return;
  try { await apiPut(`/api/alert-rules/${r.id}`, { ...r, enabled: on }); toast(`${r.name}: ${on ? 'on' : 'off'}`); loadRules(); }
  catch (e) { toast(e.message); loadRules(); }
}
async function deleteRule(id) {
  const r = S.rules.find(x => x.id === Number(id));
  if (!r || !confirm(`Delete the rule “${r.name}”? Its past alerts stay.`)) return;
  try { await apiDelete(`/api/alert-rules/${r.id}`); toast(`${r.name} deleted`); loadRules(); } catch (e) { toast(e.message); }
}

/* ════════ regions ════════ */
async function openRegions() {
  let cams = [];
  try { cams = (await apiGet('/api/regions')).cameras.map(c => c.name); } catch (e) { toast(e.message); }
  if (!ZE) ZE = ZoneEditor($('zone-editor'), {
    zoneUrl: (cam) => `/api/regions/${encodeURIComponent(cam)}/zone`,
    frameUrl: (cam, refresh) => `/api/regions/${encodeURIComponent(cam)}/frame?refresh=${refresh ? 1 : 0}`,
    onSaved: () => loadRules(),
  });
  ZE.setCameras(cams); ZE.open(ZE.camera);
}

/* ════════ the Bluetooth speaker (the host agent on the Jetson does the Bluetooth; see backend/speaker.py) ════════ */
const SPK = { timer: null, d: null, busy: false };
const ago = (t) => { if (!t) return ''; const s = Math.max(0, Math.round(Date.now() / 1000 - t));
  return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; };
async function loadSpeaker() {
  try { SPK.d = await apiGet('/api/speaker'); } catch (e) { $('spk-status').textContent = e.message; return; }
  renderSpeaker();
}
function renderSpeaker() {
  const { config: c, status: st } = SPK.d, dev = st.device;
  const tag = (ok, yes, no, warn) => `<span class="tag ${ok ? 'on_desk' : (warn ? 'warn' : 'bad')}">${ok ? yes : no}</span>`;
  $('spk-status').innerHTML = !st.agent
    ? `<span class="tag bad">Speaker service not running on the Jetson</span><span class="muted">${st.agent_age_s != null ? `last seen ${ago(Date.now() / 1000 - st.agent_age_s)}` : 'not installed here'}${st.pending ? ` · ${st.pending} job(s) waiting` : ''}</span>`
    : [tag(st.bluetooth, 'Bluetooth on', 'Bluetooth off'), tag(st.bluealsa, 'Audio ready', 'Audio service off'),
       c.device ? tag(dev && dev.connected, 'Speaker connected', dev && dev.paired ? 'Speaker not connected (it reconnects for each beep)' : 'Speaker not paired', dev && dev.paired) : '<span class="tag warn">No speaker chosen</span>',
       st.job ? `<span class="tag warn">Working: ${escapeHtml(st.job)}…</span>` : '',
       st.last_beep ? `<span class="tag ${st.last_beep.ok ? 'on_desk' : 'bad'}">Last ${st.last_beep.test ? 'test ' : ''}beep ${ago(st.last_beep.at)}: ${escapeHtml(st.last_beep.message)}</span>` : '',
       st.error ? `<span class="tag bad">${escapeHtml(st.error)}</span>` : ''].join('');
  if (!SPK.busy) {
    $('spk-enabled').checked = c.enabled;
    document.querySelectorAll('#spk-sev button').forEach(b => b.classList.toggle('active', b.dataset.v === c.severities));
    $('spk-repeat').value = String(c.repeat_s);
  }
  $('spk-device').innerHTML = c.device
    ? `${escapeHtml(c.device_name || (dev && dev.name) || 'Bluetooth speaker')}<small class="mono">${escapeHtml(c.device)}</small>`
    : '<span class="muted">none — Find speakers, then Connect</span>';
  $('spk-forget').hidden = !c.device;
  $('spk-test').disabled = !c.device;
  const sc = st.scan || {};
  $('spk-scan').disabled = !!sc.running || st.job === 'scan';
  $('spk-scan').lastChild.textContent = sc.running || st.job === 'scan' ? 'Searching… (12 s)' : 'Find speakers';
  const box = $('spk-found');
  if (sc.at && (sc.devices || []).length) {
    box.hidden = false;
    box.innerHTML = sc.devices.map(d => `<div class="spk-row"><span data-icon="${d.audio ? 'speaker' : 'info'}"></span>
        <div class="grow"><b>${escapeHtml(d.name)}</b><small class="mono">${escapeHtml(d.mac)}${d.audio ? ' · audio' : ''}${d.paired ? ' · paired' : ''}${d.connected ? ' · connected' : ''}</small></div>
        ${d.mac === c.device ? '<span class="tag on_desk">In use</span>' : `<button class="btn sm" data-connect="${escapeHtml(d.mac)}" data-name="${escapeHtml(d.name)}">Connect</button>`}</div>`).join('');
    hydrateIcons && hydrateIcons(box);
  } else if (sc.at) { box.hidden = false; box.innerHTML = '<div class="spk-row muted">No Bluetooth device found — is the speaker in pairing mode?</div>'; }
  else box.hidden = true;
}
async function speakerSave(patch) {
  SPK.busy = true;
  try { SPK.d = await apiPut('/api/speaker', patch); toast('Saved'); }
  catch (e) { toast(e.message); }
  SPK.busy = false; renderSpeaker();
}
async function speakerJob(path, body = {}, msg) {
  try { SPK.d = await apiPost(path, body); if (msg) toast(msg); renderSpeaker(); } catch (e) { toast(e.message); }
}

/* ════════ start ════════ */
onReady(() => {
  document.querySelectorAll('[data-need]').forEach(e => { if (!can(e.dataset.need)) e.hidden = true; });
  initSearch({ placeholder: 'Search rule, message, camera, region…', onFilter: (q) => { S.q = q; alPager.reset(); loadHistory(); } });
  document.querySelectorAll('#al-tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
  const segFilter = (id, key, attr) => document.querySelectorAll(`#${id} button`).forEach(b => b.addEventListener('click', () => {
    S[key] = b.dataset[attr]; document.querySelectorAll(`#${id} button`).forEach(x => x.classList.toggle('active', x === b));
    alPager.reset(); loadHistory(); }));
  segFilter('sev-filter', 'sev', 'sev'); segFilter('status-filter', 'status', 'status');
  $('cam-filter').addEventListener('change', (e) => { S.camera = e.target.value; alPager.reset(); loadHistory(); });
  $('ack-all').addEventListener('click', ackAll);
  $('al-body').addEventListener('click', (e) => {
    const a = e.target.closest('[data-ack]'); if (a) return ack(a.dataset.ack);
    const s = e.target.closest('[data-snap]'); if (s) openSnap(s.dataset.snap);
  });
  $('snap-close').addEventListener('click', () => { $('snap-modal').hidden = true; });
  $('snap-modal').addEventListener('click', (e) => { if (e.target.id === 'snap-modal') $('snap-modal').hidden = true; });
  // rules
  $('rule-new').addEventListener('click', () => openRule(null));
  $('rule-body').addEventListener('click', (e) => {
    const ed = e.target.closest('[data-edit]'); if (ed) return openRule(S.rules.find(r => r.id === Number(ed.dataset.edit)));
    const del = e.target.closest('[data-del]'); if (del) deleteRule(del.dataset.del);
  });
  $('rule-body').addEventListener('change', (e) => { const t = e.target.closest('[data-toggle]'); if (t) toggleRule(t.dataset.toggle, t.checked); });
  $('rule-form').addEventListener('submit', saveRule);
  // speaker
  $('spk-enabled').addEventListener('change', (e) => speakerSave({ enabled: e.target.checked }));
  document.querySelectorAll('#spk-sev button').forEach(b => b.addEventListener('click', () => speakerSave({ severities: b.dataset.v })));
  $('spk-repeat').addEventListener('change', (e) => speakerSave({ repeat_s: Number(e.target.value) }));
  $('spk-test').addEventListener('click', () => speakerJob('/api/speaker/test', { severity: 'critical' }, 'Test beep sent to the speaker'));
  $('spk-scan').addEventListener('click', () => speakerJob('/api/speaker/scan', {}, 'Searching for speakers (12 s)…'));
  $('spk-forget').addEventListener('click', () => { if (confirm('Remove this speaker from the Jetson? The beep switches off.')) speakerJob('/api/speaker/forget', {}, 'Speaker removed'); });
  $('spk-found').addEventListener('click', (e) => { const b = e.target.closest('[data-connect]');
    if (b) speakerJob('/api/speaker/connect', { mac: b.dataset.connect, name: b.dataset.name }, `Connecting ${b.dataset.name}… (up to 40 s)`); });
  $('rule-form').addEventListener('input', syncForm);
  $('rule-form').addEventListener('change', syncForm);
  $('rule-cams').addEventListener('change', (e) => {
    const all = $('rule-cams').querySelector('[data-allcams]');
    if (e.target === all && all.checked) $('rule-cams').querySelectorAll('input:not([data-allcams])').forEach(x => { x.checked = false; });
    else if (e.target !== all) all.checked = ![...$('rule-cams').querySelectorAll('input:not([data-allcams])')].some(x => x.checked);
    syncForm();
  });
  document.querySelectorAll('#rule-sev button').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('#rule-sev button').forEach(x => x.classList.toggle('active', x === b)); syncForm(); }));
  $('rule-close').addEventListener('click', closeRule); $('rule-cancel').addEventListener('click', closeRule);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { if (!$('rule-modal').hidden) closeRule(); $('snap-modal').hidden = true; } });
  // live: a new alert reloads the list
  window.addEventListener('perimeter-alert', () => { if (S.tab === 'history') loadHistory(); });
  setInterval(() => { if (S.tab === 'history' && !document.hidden) loadHistory(); }, 15000);
  loadRules();
  initRangeControl((st) => { S.range = st; alPager.reset(); loadHistory(); });
  const t = location.hash.slice(1);
  showTab(['history', 'rules', 'regions', 'speaker'].includes(t) ? t : 'history');
});
