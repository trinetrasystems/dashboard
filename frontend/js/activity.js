/* Activity Log — admin only. Every value from the server is inserted as text, never as HTML. */
(() => {
  const PAGE = 50;
  const REFRESH_MS = 15000;

  // action → [label, tone]
  const ACTIONS = {
    login:                  ['Signed in', 't-ok'],
    logout:                 ['Signed out', 't-info'],
    login_failed:           ['Wrong password', 't-danger'],
    login_lockout:          ['Account locked', 't-danger'],
    login_refused_locked:   ['Sign-in refused (locked)', 't-danger'],
    password_changed:       ['Changed own password', 't-change'],
    password_change_failed: ['Password change failed', 't-danger'],
    password_reset:         ['Reset a password', 't-change'],
    user_created:           ['Created account', 't-change'],
    user_deleted:           ['Deleted account', 't-danger'],
    settings_changed:       ['Changed settings', 't-change'],
    alert_ack:              ['Acknowledged alert', 't-info'],
    whitelist_add:          ['Whitelisted class', 't-change'],
    whitelist_remove:       ['Removed from whitelist', 't-change'],
    record_deleted:         ['Deleted record', 't-danger'],
    table_cleared:          ['Cleared table', 't-danger'],
    full_reset:             ['Full data reset', 't-danger'],
    detections_export:      ['Exported detections', 't-info'],
    activity_export:        ['Exported activity log', 't-info'],
    test_detection:         ['Test detection', 't-demo'],
    schedule_add:           ['Scheduled demo alert', 't-demo'],
    schedule_cancel:        ['Cancelled demo alert', 't-demo'],
    schedule_cancel_date:   ['Cancelled demo day', 't-demo'],
    schedule_copy:          ['Copied demo day', 't-demo'],
    page_view:              ['Opened page', 't-view'],
  };

  const $ = (id) => document.getElementById(id);
  const state = { offset: 0, total: 0, timer: null, loading: false };

  /* ── small DOM helper: el('td', {class: 'x'}, 'text', childNode) ── */
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k.startsWith('data-') || k === 'colspan') n.setAttribute(k, v);
      else n[k] = v;
    }
    for (const kid of kids) {
      if (kid === null || kid === undefined) continue;
      n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }

  /* ── server calls ── */
  async function api(path) {
    const r = await fetch(path, { credentials: 'same-origin' });
    if (r.status === 401) { location.replace('/admin.html'); throw new Error('signed out'); }
    if (r.status === 403) { location.replace('/index.html'); throw new Error('not admin'); }
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || `Request failed (${r.status})`);
    }
    return r.json();
  }

  function showError(msg) {
    const e = $('err');
    e.textContent = msg || '';
    e.style.display = msg ? 'block' : 'none';
  }

  /* ── time helpers: the server stores UTC 'YYYY-MM-DD HH:MM:SS' ── */
  const fromUtc = (s) => new Date(String(s).replace(' ', 'T') + 'Z');
  const toUtc = (d) => d.toISOString().slice(0, 19).replace('T', ' ');
  const fmt = (d) => d.toLocaleString(undefined, { day: '2-digit', month: 'short', year: 'numeric',
                                                    hour: '2-digit', minute: '2-digit', second: '2-digit' });
  function ago(d) {
    const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
    if (s < 45) return 'just now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  function whenCell(d, label = 'Time') {
    return el('td', { class: 'mono nowrap', title: d.toString(), 'data-label': label },
      el('div', {}, fmt(d), el('span', { class: 'when-rel' }, ago(d))));
  }

  /* ── 'Chrome · Android' from a user-agent string ── */
  function browser(ua) {
    if (!ua) return '—';
    const b = /Edg\//.test(ua) ? 'Edge' : /OPR\//.test(ua) ? 'Opera' : /SamsungBrowser/.test(ua) ? 'Samsung Internet'
      : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari'
      : (ua.split(/[\/\s]/)[0] || 'Other');
    const os = /Android/.test(ua) ? 'Android' : /iPhone|iPad|iPod/.test(ua) ? 'iOS' : /Windows/.test(ua) ? 'Windows'
      : /Mac OS X|Macintosh/.test(ua) ? 'macOS' : /CrOS/.test(ua) ? 'ChromeOS' : /Linux/.test(ua) ? 'Linux' : '';
    return os ? `${b} · ${os}` : b;
  }

  function roleTag(role) {
    return el('span', { class: 'role ' + (role || 'unknown') }, role || 'no account');
  }

  function userButton(uid, role) {
    if (!uid || uid === 'system') return el('span', { class: 'dim mono' }, 'system (automatic)');
    return el('span', {},
      el('button', { class: 'user-link', type: 'button', 'data-user': uid, title: 'Show only this user' }, uid),
      roleTag(role));
  }

  /* ── summary: tiles, signed in now, accounts ── */
  let summaryLoaded = false;
  async function loadSummary() {
    const s = await api('/api/admin/audit/summary');
    const h = s.last_24h;
    $('t-signins').textContent = h.sign_ins;
    $('t-failed').textContent = h.failed_sign_ins;
    $('t-locked').textContent = h.lockouts;
    $('t-changes').textContent = h.changes;
    $('t-acks').textContent = h.alerts_acknowledged;
    $('t-views').textContent = h.page_views;
    $('tile-failed').classList.toggle('warn', h.failed_sign_ins > 0);
    $('tile-locked').classList.toggle('warn', h.lockouts > 0);
    $('page-sub').textContent = 'Every sign-in, change and page visit by every account · times shown in your local time · '
      + (s.retention_days > 0 ? `kept for ${s.retention_days} days` : 'kept forever');

    const online = $('online');
    online.replaceChildren();
    if (!s.online.length) online.append(el('tr', {}, el('td', { colspan: 5, class: 'empty' }, 'Nobody is signed in')));
    for (const o of s.online) {
      online.append(el('tr', {},
        el('td', { 'data-label': 'User' }, userButton(o.user_id, o.role)),
        whenCell(new Date(o.created * 1000), 'Signed in'),
        el('td', { class: 'mono nowrap', 'data-label': 'Active' }, ago(new Date(o.last_seen * 1000))),
        el('td', { class: 'mono nowrap', 'data-label': 'IP' }, o.ip || '—'),
        el('td', { class: 'mono nowrap', title: o.user_agent || '', 'data-label': 'Browser' }, browser(o.user_agent))));
    }

    const acc = $('accounts');
    acc.replaceChildren();
    if (!s.accounts.length) acc.append(el('tr', {}, el('td', { colspan: 2, class: 'empty' }, 'No accounts')));
    for (const a of s.accounts) {
      acc.append(el('tr', {},
        el('td', {}, userButton(a.user_id, a.role)),
        a.last_login ? whenCell(fromUtc(a.last_login)) : el('td', { class: 'dim mono' }, 'never')));
    }

    if (!summaryLoaded) {            // fill the filter lists once
      summaryLoaded = true;
      const u = $('f-user');
      for (const a of s.accounts) u.append(el('option', { value: a.user_id }, `${a.user_id} (${a.role})`));
      const c = $('f-cat');
      for (const [k, label] of Object.entries(s.categories)) c.append(el('option', { value: k }, label));
    }
  }

  /* ── filters → query string ── */
  function filterParams() {
    const p = new URLSearchParams();
    const user = $('f-user').value, cat = $('f-cat').value, q = $('f-q').value.trim();
    if (user) p.set('actor', user);
    if (cat) p.set('category', cat);
    if (q) p.set('q', q);
    if ($('f-from').value) p.set('since', toUtc(new Date($('f-from').value + 'T00:00:00')));
    if ($('f-to').value) {
      const end = new Date($('f-to').value + 'T00:00:00');
      end.setDate(end.getDate() + 1);              // the whole 'to' day, in local time
      p.set('until', toUtc(end));
    }
    if ($('f-hide').checked) p.set('hide_pages', 'true');
    return p;
  }

  /* ── the log table ── */
  async function loadLog() {
    if (state.loading) return;
    state.loading = true;
    try {
      const p = filterParams();
      p.set('limit', PAGE);
      p.set('offset', state.offset);
      const d = await api('/api/admin/audit?' + p.toString());
      state.total = d.total;
      const body = $('log');
      body.replaceChildren();
      if (!d.records.length) {
        body.append(el('tr', {}, el('td', { colspan: 6, class: 'empty' },
          state.total ? 'No more entries' : 'No activity matches these filters')));
      }
      for (const r of d.records) {
        const [label, tone] = ACTIONS[r.action] || [r.action, 't-info'];
        let detail = r.detail || '';
        if (r.action === 'login_failed') detail = `Someone tried to sign in as ${detail}`;
        if (r.action === 'login_refused_locked') detail = `Sign-in attempt as ${detail} while it was locked`;
        body.append(el('tr', { class: tone === 't-danger' ? 'row-danger' : '' },
          whenCell(fromUtc(r.at)),
          el('td', { 'data-label': 'User' }, userButton(r.actor, r.role)),
          el('td', { 'data-label': 'Activity' }, el('span', { class: 'badge ' + tone }, label)),
          el('td', { class: 'detail', 'data-label': 'Details' }, detail),
          el('td', { class: 'mono nowrap', 'data-label': 'IP' }, r.ip || '—'),
          el('td', { class: 'mono nowrap', title: r.user_agent || '', 'data-label': 'Browser' }, browser(r.user_agent))));
      }
      const from = state.total ? state.offset + 1 : 0;
      const to = Math.min(state.offset + PAGE, state.total);
      $('count').textContent = `${state.total.toLocaleString()} ${state.total === 1 ? 'entry' : 'entries'}`;
      $('page-info').textContent = state.total ? `${from}–${to} of ${state.total.toLocaleString()}` : '';
      $('newer').disabled = state.offset === 0;
      $('older').disabled = state.offset + PAGE >= state.total;
      showError('');
    } finally {
      state.loading = false;
    }
  }

  async function refresh() {
    try {
      await Promise.all([loadSummary(), state.offset === 0 ? loadLog() : Promise.resolve()]);
    } catch (e) {
      if (e.message !== 'signed out' && e.message !== 'not admin') showError(e.message || 'Cannot reach the server');
    }
  }

  function applyFilters() {
    state.offset = 0;
    loadLog().catch((e) => showError(e.message));
  }

  function setAuto(on) {
    clearInterval(state.timer);
    state.timer = on ? setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS) : null;
    $('live-dot').classList.toggle('on', on);
  }

  /* ── wiring ── */
  ['f-user', 'f-cat', 'f-from', 'f-to', 'f-hide'].forEach((id) => $(id).addEventListener('change', applyFilters));
  let qTimer;
  $('f-q').addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(applyFilters, 350); });

  $('clear').addEventListener('click', () => {
    ['f-user', 'f-cat', 'f-from', 'f-to', 'f-q'].forEach((id) => { $(id).value = ''; });
    $('f-hide').checked = false;
    applyFilters();
  });

  $('newer').addEventListener('click', () => { state.offset = Math.max(0, state.offset - PAGE); loadLog().catch((e) => showError(e.message)); });
  $('older').addEventListener('click', () => { state.offset += PAGE; loadLog().catch((e) => showError(e.message)); });

  $('export').addEventListener('click', () => {
    window.location.href = '/api/admin/audit/export?' + filterParams().toString();
  });

  // Click any user name to show only that user's activity.
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-user]');
    if (!b) return;
    const sel = $('f-user'), uid = b.getAttribute('data-user');
    if (![...sel.options].some((o) => o.value === uid)) sel.append(el('option', { value: uid }, uid));
    sel.value = uid;
    applyFilters();
    $('f-user').scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  $('auto').addEventListener('change', (e) => setAuto(e.target.checked));
  document.addEventListener('visibilitychange', () => { if (!document.hidden && $('auto').checked) refresh(); });

  $('sign-out').addEventListener('click', async () => {
    try { await fetch('/api/admin/logout', { method: 'POST', credentials: 'same-origin' }); } catch (_) { /* ignore */ }
    location.replace('/admin.html');
  });

  api('/api/me').then((m) => { $('me').textContent = m.user_id || ''; }).catch(() => {});
  refresh();
  setAuto(true);
})();
