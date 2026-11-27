/* ────────────────────────────────────────────────────────────────────────────
 * ZONE EDITOR — the perimeter line (+ its outside side) and named regions drawn on a camera's picture.
 * Used by DeepStream → Perimeter zones (Trinetra team) and Alerts → Regions (the client's admins).
 *   ZoneEditor(root, { cameras: [names], zoneUrl: cam => url, frameUrl: (cam, refresh) => url, onSaved, team })
 * Regions: alert (anything there = breach), watch (the alert rules decide), ignore (never reported) and — drawn by the
 * Trinetra team only (team: true, the DeepStream page) — near (near the wall: a watch region the throw / loitering logic
 * also uses), band (throw band: the strip along the wall top watched for thrown objects, every frame) and landing
 * (landing zone: the ground inside where a thrown / left object is looked for). The client's editor shows those three
 * but cannot change them (the server keeps them as they are).
 * Points are fractions of the frame; the outside-side rule is the same as backend/pipeline/zones.py.
 * ──────────────────────────────────────────────────────────────────────────── */
function ZoneEditor(root, opts) {
  const COL = { line: '#f59e0b', alert: '#ef4444', watch: '#3b82f6', ignore: '#8a8f99', draft: '#14b8a6',
                near: '#a855f7', band: '#facc15', landing: '#84cc16' };
  const KIND = { alert: 'Alert region', watch: 'Watch region', ignore: 'Ignore region', near: 'Near-wall region',
                 band: 'Throw band', landing: 'Landing zone' };
  const SHORT = { alert: 'alert', watch: 'watch', ignore: 'ignore', near: 'near wall', band: 'throw band', landing: 'landing zone' };
  const TEAM_KINDS = ['near', 'band', 'landing'], REGION_TOOLS = ['alert', 'watch', 'ignore', ...TEAM_KINDS];
  const team = !!opts.team;
  const HELP = {
    null: 'Pick a tool, then click on the picture.',
    line: '<b>Perimeter line:</b> click along the wall or fence, point by point. <b>Double-click</b> or <b>Enter</b> to finish. A new line replaces the old one.',
    outside: '<b>Outside side:</b> click once anywhere outside the perimeter line (the arrows then point outside).',
    alert: '<b>Alert region</b> (wall top, concertina wire — anything there is a breach): click its corners; click the <b>first point</b> or <b>double-click</b> to close it, then name it.',
    watch: '<b>Watch region</b> (a gate, a parking area — your alert rules decide): click its corners, close it, name it.',
    ignore: '<b>Ignore region</b> (a road, a moving tree — never reported): click its corners, close it, name it.',
    near: '<b>Near-wall region</b> (the ground just inside the wall): a person here raises a throw alert\'s level and can be a loitering rule\'s region. Click its corners, close it, name it.',
    band: '<b>Throw band</b> (thrown objects): a strip along the wall top, a little above and below it — the perimeter line must run through it. Every frame is checked here for a small fast object crossing the line from outside to inside. Keep it tight: trees, the road and lights must stay out.',
    landing: '<b>Landing zone</b> (thrown / left objects): the ground inside where something thrown would land. Checked for new objects about once a second, against pictures of the normal scene. Leave out moving things (trees, a busy path).',
  };
  const S = { cam: null, zone: null, draft: null, tool: null, hist: [], dirty: false, hover: null };
  const empty = () => ({ line: [], outside: null, regions: [] });
  const id = (x) => root.querySelector(`[data-ze="${x}"]`);

  root.innerHTML = `
    <div class="zone-bar">
      <select class="ds-input" data-ze="cam" aria-label="Camera"></select>
      <button class="btn sm" data-ze="refresh" title="Take a new picture from the camera">↻ New picture</button>
      <div class="seg zone-tools" data-ze="tools">
        <button data-tool="line">Perimeter line</button>
        <button data-tool="outside">Outside side</button>
        <button data-tool="alert">Alert region</button>
        <button data-tool="watch">Watch region</button>
        <button data-tool="ignore">Ignore region</button>
        ${team ? `<button data-tool="near" title="Trinetra team">Near wall</button>
        <button data-tool="band" title="Trinetra team: throw detection">Throw band</button>
        <button data-tool="landing" title="Trinetra team: landing / left-object detection">Landing zone</button>` : ''}
      </div>
      <button class="btn sm" data-ze="undo" title="Remove the last point (Ctrl+Z)">Undo</button>
      <button class="btn sm danger-ghost" data-ze="clear">Clear all</button>
      <span class="spacer" style="flex:1"></span>
      <span class="form-result" data-ze="msg"></span>
      <button class="btn primary" data-ze="save">Save</button>
    </div>
    <div class="zone-help"><div data-ze="help">${HELP.null}</div><div class="zone-pointer mono" data-ze="pointer">&nbsp;</div></div>
    <div class="zone-wrap">
      <div class="zone-stage" data-ze="stage">
        <img data-ze="img" alt="Camera picture">
        <canvas data-ze="canvas"></canvas>
        <div class="zone-empty" data-ze="empty" hidden></div>
      </div>
      <div class="zone-side">
        <b>What is drawn</b>
        <div class="zone-list" data-ze="list"></div>
        <b>What a detection means</b>
        <div class="legend">
          <div><span class="sw" style="background:${COL.alert}"></span>In an <b>alert region</b> → breach (rule “Perimeter breach”)</div>
          <div><span class="sw" style="background:${COL.line}"></span><b>Outside the line</b> → the outside rules (e.g. 10 min)</div>
          <div><span class="sw" style="background:${COL.watch}"></span>In a <b>watch region</b> → the alert rules that name it</div>
          <div><span class="sw" style="background:#22c55e"></span>Inside, no region → not reported</div>
          <div><span class="sw" style="background:${COL.ignore}"></span>In an <b>ignore region</b> → never reported</div>
          <div><span class="sw" style="background:${COL.near}"></span>In a <b>near-wall region</b> → as a watch region; also raises a throw alert</div>
          <div><span class="sw" style="background:${COL.band}"></span><b>Throw band</b> → watched for thrown objects (not for people)</div>
          <div><span class="sw" style="background:${COL.landing}"></span><b>Landing zone</b> → watched for new / left objects (not for people)</div>
          <div>The point that counts is the bottom centre of each box (where the person stands).</div>
        </div>
      </div>
    </div>
    <div class="modal-backdrop" data-ze="name-modal" hidden>
      <form class="modal form" data-ze="name-form" autocomplete="off">
        <div class="card-head"><h2 class="card-title" data-ze="name-title">Name this region</h2></div>
        <label>Name <input name="name" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9 ._\\-]{0,39}"
          title="1–40 letters, numbers, spaces, dots, dashes"></label>
        <div class="form-result bad" data-ze="name-msg"></div>
        <div class="pop-actions"><button type="button" class="btn ghost" data-ze="name-cancel">Cancel</button>
          <button type="submit" class="btn primary">Save name</button></div>
      </form>
    </div>`;

  const regions = () => S.zone.regions;
  const msg = (t, ok) => { const e = id('msg'); e.className = `form-result ${ok ? 'good' : 'bad'}`; e.textContent = t; };

  /* ─── geometry (zones.py) ─── */
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
  function meaning(p) {
    const z = S.zone;
    if (regions().some(r => r.kind === 'ignore' && inPoly(p, r.points))) return 'ignored';
    const names = regions().filter(r => !['ignore', 'band', 'landing'].includes(r.kind) && inPoly(p, r.points));
    const o = isOutside(p, z);
    const parts = [];
    if (names.some(r => r.kind === 'alert')) parts.push('BREACH (alert region)');
    names.filter(r => r.kind === 'watch' || r.kind === 'near').forEach(r => parts.push(`in ${r.kind === 'near' ? 'near-wall' : 'watch'} region “${r.name}”`));
    if (o === true) parts.push('OUTSIDE the line');
    regions().filter(r => (r.kind === 'band' || r.kind === 'landing') && inPoly(p, r.points))
      .forEach(r => parts.push(r.kind === 'band' ? `throw band “${r.name}” (thrown objects)` : `landing zone “${r.name}” (new objects)`));
    if (!parts.length) return o === null && !regions().length ? 'draw the perimeter line, its outside side and the regions' : 'inside → not reported';
    return parts.join(' · ');
  }

  /* ─── drawing ─── */
  function sizeCanvas() {
    const c = id('canvas'), img = id('img'), r = devicePixelRatio || 1;
    const w = img.clientWidth || id('stage').clientWidth, h = img.clientHeight || Math.round(w * 9 / 16);
    c.width = Math.round(w * r); c.height = Math.round(h * r);
    if (!img.naturalWidth) id('stage').style.minHeight = `${h}px`;
  }
  const centroid = (pts) => [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];
  function draw() {
    const c = id('canvas'), ctx = c.getContext('2d'), W = c.width, H = c.height, r = devicePixelRatio || 1, z = S.zone || empty();
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
    const fill = { alert: 'rgba(239,68,68,.28)', watch: 'rgba(59,130,246,.25)', ignore: 'rgba(138,143,153,.35)',
                   near: 'rgba(168,85,247,.22)', band: 'rgba(250,204,21,.16)', landing: 'rgba(132,204,22,.14)' };
    z.regions.forEach(rg => { poly(rg.points, fill[rg.kind], COL[rg.kind], true, rg.kind === 'band' || rg.kind === 'landing' ? [8, 5] : []);
                              label(rg.name, centroid(rg.points), '#fff'); });
    if (z.line.length >= 2) {
      poly(z.line, null, COL.line, false); dots(z.line, COL.line);
      if (z.outside) {
        for (let i = 0; i < z.line.length - 1; i++) {
          const a = z.line[i], b = z.line[i + 1], m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
          const dxp = (b[0] - a[0]) * W, dyp = (b[1] - a[1]) * H, L = Math.hypot(dxp, dyp) || 1;
          let n = [-dyp / L * 34 * r, dxp / L * 34 * r];
          const test = [m[0] + n[0] / W * 0.5, m[1] + n[1] / H * 0.5];
          if (!isOutside(test, z)) n = [-n[0], -n[1]];
          const [mx, my] = P(m); arrow(ctx, mx, my, mx + n[0], my + n[1], COL.line, r);
        }
      }
    } else if (z.line.length === 1) dots(z.line, COL.line);
    if (z.outside) { const [x, y] = P(z.outside); ctx.beginPath(); ctx.arc(x, y, 7 * r, 0, 7); ctx.fillStyle = COL.line; ctx.fill();
                     label('OUTSIDE', [z.outside[0], Math.max(0.03, z.outside[1] - 0.05)], COL.line); }
    if (S.draft && S.draft.pts.length) {
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

  function renderList() {
    const z = S.zone || empty(), rows = [];
    rows.push(`<div class="zone-item"><span><span class="sw" style="background:${COL.line}"></span>Perimeter line · ${z.line.length >= 2 ? `${z.line.length} points` : '<i>not drawn</i>'}</span>
      ${z.line.length ? '<button class="btn sm danger-ghost" data-zdel="line" title="Delete">✕</button>' : ''}</div>`);
    rows.push(`<div class="zone-item"><span><span class="sw" style="background:${COL.line}"></span>Outside side · ${z.outside ? 'marked' : '<i>not marked</i>'}</span>
      ${z.outside ? '<button class="btn sm danger-ghost" data-zdel="outside" title="Delete">✕</button>' : ''}</div>`);
    z.regions.forEach((rg, i) => {
      const locked = !team && TEAM_KINDS.includes(rg.kind);           // the team's: shown, not changed here
      rows.push(`<div class="zone-item"><span><span class="sw" style="background:${COL[rg.kind] || '#999'}"></span>
      ${locked ? `<b>${escapeHtml(rg.name)}</b>` : `<button class="link-btn" data-zren="${i}" title="Rename">${escapeHtml(rg.name)}</button>`}
      · ${SHORT[rg.kind] || rg.kind} · ${rg.points.length} corners${locked ? ' · <span class="muted">set by Trinetra</span>' : ''}</span>
      ${locked ? '' : `<button class="btn sm danger-ghost" data-zdel="r:${i}" title="Delete">✕</button>`}</div>`);
    });
    id('list').innerHTML = rows.join('');
  }
  function remember() { S.hist.push(JSON.stringify(S.zone)); if (S.hist.length > 60) S.hist.shift(); S.dirty = true; msg('not saved', false); }
  const changed = () => { renderList(); draw(); };

  /* ─── naming ─── */
  function askName(title, value) {
    return new Promise(res => {
      const m = id('name-modal'), f = id('name-form');
      id('name-title').textContent = title; f.elements.name.value = value; id('name-msg').textContent = '';
      m.hidden = false; f.elements.name.focus(); f.elements.name.select();
      const done = (v) => { m.hidden = true; f.onsubmit = null; id('name-cancel').onclick = null; res(v); };
      id('name-cancel').onclick = () => done(null);
      f.onsubmit = (e) => {
        e.preventDefault();
        const v = f.elements.name.value.trim();
        if (S.zone.regions.some(r => r.name.toLowerCase() === v.toLowerCase() && r.name !== value)) { id('name-msg').textContent = 'Another region already has this name.'; return; }
        done(v);
      };
    });
  }
  function nextName(kind) {
    const base = { alert: 'Alert', watch: 'Gate', ignore: 'Ignore', near: 'Near wall', band: 'Band', landing: 'Landing' }[kind];
    for (let i = 1; ; i++) if (!S.zone.regions.some(r => r.name.toLowerCase() === `${base} ${i}`.toLowerCase())) return `${base} ${i}`;
  }

  async function finishDraft() {
    const d = S.draft; S.draft = null;
    if (!d) return;
    if (d.tool === 'line' && d.pts.length >= 2) { remember(); S.zone.line = d.pts; }
    if (REGION_TOOLS.includes(d.tool) && d.pts.length >= 3) {
      changed();
      const name = await askName(`Name this ${KIND[d.tool].toLowerCase()}`, nextName(d.tool));
      if (name) { remember(); S.zone.regions.push({ name, kind: d.tool, points: d.pts }); }
    }
    changed();
  }
  const pointAt = (ev) => { const rc = id('canvas').getBoundingClientRect();
    return [Math.min(1, Math.max(0, (ev.clientX - rc.left) / rc.width)), Math.min(1, Math.max(0, (ev.clientY - rc.top) / rc.height))]; };
  const nearPx = (a, b) => { const rc = id('canvas').getBoundingClientRect(); return Math.hypot((a[0] - b[0]) * rc.width, (a[1] - b[1]) * rc.height); };

  function setTool(t) {
    finishDraft();
    S.tool = S.tool === t ? null : t;
    id('tools').querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.tool === S.tool));
    id('help').innerHTML = HELP[S.tool];
  }
  function onClick(ev) {
    if (!S.tool || !S.cam || !id('name-modal').hidden) return;
    const p = pointAt(ev);
    if (S.tool === 'outside') { remember(); S.zone.outside = p; changed(); return; }
    if (!S.draft) S.draft = { tool: S.tool, pts: [] };
    const pts = S.draft.pts;
    if (S.tool !== 'line' && pts.length >= 3 && nearPx(p, pts[0]) < 12) { finishDraft(); return; }
    if (pts.length && nearPx(p, pts[pts.length - 1]) < 3) return;      // the second click of a double-click
    pts.push(p); draw();
  }
  function onMove(ev) {
    const p = pointAt(ev); S.hover = S.draft ? p : null;
    if (S.draft) draw();
    if (S.zone) id('pointer').textContent = `Pointer: a person standing here → ${meaning(p)}`;
  }
  function undo() {
    if (S.draft && S.draft.pts.length) { S.draft.pts.pop(); if (!S.draft.pts.length) S.draft = null; draw(); return; }
    if (!S.hist.length) return;
    S.zone = JSON.parse(S.hist.pop()); S.dirty = true; changed();
  }
  async function save() {
    await finishDraft();
    if (S.zone.line.length >= 2 && !S.zone.outside) { msg('Mark the outside side first (tool “Outside side”).', false); return; }
    if (S.zone.regions.some(r => r.kind === 'band') && S.zone.line.length < 2) { msg('A throw band needs the perimeter line (the wall top) through it.', false); return; }
    try {
      const out = await apiPut(opts.zoneUrl(S.cam), S.zone);
      S.zone = { ...empty(), ...out.zone }; S.dirty = false; S.hist = [];
      msg(`Saved — used by the pipeline ${out.applied}.`, true); changed();
      opts.onSaved && opts.onSaved(S.cam, S.zone);
    } catch (e) { msg(e.message, false); }
  }
  function showEmpty(text) { const e = id('empty'); e.hidden = false; e.textContent = text; }

  async function open(cam, refresh = false) {
    if (S.dirty && cam !== S.cam && !confirm('The zones of this camera are not saved. Leave them?')) { id('cam').value = S.cam; return; }
    S.cam = cam || S.cam; S.draft = null; S.dirty = false; S.hist = []; id('msg').textContent = '';
    if (!S.cam) { S.zone = empty(); showEmpty('No camera yet: cameras are added on the DeepStream page (Trinetra team).'); changed(); return; }
    try {
      const z = await apiGet(opts.zoneUrl(S.cam));
      S.zone = { ...empty(), ...z, regions: [...(z.regions || []),
        ...(z.alert || []).map((p, i) => ({ name: `Alert ${i + 1}`, kind: 'alert', points: p })),
        ...(z.ignore || []).map((p, i) => ({ name: `Ignore ${i + 1}`, kind: 'ignore', points: p }))] };
      delete S.zone.alert; delete S.zone.ignore;
    } catch (_) { S.zone = empty(); }
    showEmpty('Taking a picture from the camera…');
    const img = id('img');
    img.onload = () => { id('empty').hidden = true; id('stage').style.minHeight = ''; sizeCanvas(); draw(); };
    img.onerror = async () => {
      let why = 'no picture from this camera';
      try { const r = await fetch(img.src, { credentials: 'same-origin' }); why = (await r.json()).detail || why; } catch (_) { }
      showEmpty(`${why}\nCheck the camera, then “New picture”.`); sizeCanvas(); draw();
    };
    img.src = opts.frameUrl(S.cam, refresh) + `${opts.frameUrl(S.cam, refresh).includes('?') ? '&' : '?'}t=${Date.now()}`;
    changed();
  }
  function setCameras(names, pick) {
    const sel = id('cam');
    sel.innerHTML = names.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('') || '<option value="">no cameras</option>';
    S.cam = (pick && names.includes(pick)) ? pick : (names.includes(S.cam) ? S.cam : names[0] || null);
    sel.value = S.cam || '';
  }

  /* ─── wiring ─── */
  id('cam').addEventListener('change', (e) => open(e.target.value));
  id('refresh').addEventListener('click', () => open(S.cam, true));
  id('tools').querySelectorAll('button').forEach(b => b.addEventListener('click', () => setTool(b.dataset.tool)));
  id('undo').addEventListener('click', undo);
  id('clear').addEventListener('click', () => {
    if (!confirm(team ? 'Clear every zone of this camera? (Save to keep it that way.)'
                      : 'Clear the perimeter line and your regions of this camera? (Trinetra\'s regions stay. Save to keep it that way.)')) return;
    remember(); S.zone = { ...empty(), regions: team ? [] : S.zone.regions.filter(r => TEAM_KINDS.includes(r.kind)) }; S.draft = null; changed(); });
  id('save').addEventListener('click', save);
  id('list').addEventListener('click', async (e) => {
    const ren = e.target.closest('[data-zren]');
    if (ren) {
      const rg = S.zone.regions[Number(ren.dataset.zren)];
      const name = await askName('Rename the region', rg.name);
      if (name && name !== rg.name) { remember(); rg.name = name; changed(); }
      return;
    }
    const b = e.target.closest('[data-zdel]'); if (!b) return; remember();
    const k = b.dataset.zdel;
    if (k === 'line') S.zone.line = []; else if (k === 'outside') S.zone.outside = null; else S.zone.regions.splice(Number(k.split(':')[1]), 1);
    changed();
  });
  const cv = id('canvas');
  cv.addEventListener('click', onClick);
  cv.addEventListener('dblclick', (e) => { e.preventDefault(); finishDraft(); });
  cv.addEventListener('mousemove', onMove);
  cv.addEventListener('mouseleave', () => { S.hover = null; id('pointer').innerHTML = '&nbsp;'; draw(); });
  document.addEventListener('keydown', (e) => {
    if (!root.offsetParent || e.target.closest('input,select,textarea')) return;
    if (e.key === 'Enter') finishDraft();
    if (e.key === 'Escape') { S.draft = null; draw(); }
    if ((e.ctrlKey || e.metaKey) && e.key === 'z') { e.preventDefault(); undo(); }
  });
  window.addEventListener('resize', () => { sizeCanvas(); draw(); });
  window.addEventListener('beforeunload', (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });
  return { open, setCameras, get camera() { return S.cam; }, get dirty() { return S.dirty; } };
}
