/* ────────────────────────────────────────────────────────────────────────────
 * NotificationService — FCM web push for the Trinetra dashboard.
 *
 * Order matters: a token is only ever requested AFTER the backend confirms a
 * signed-in user, so a device can never be registered anonymously or against
 * somebody else's userId. The backend reads the userId from the session
 * cookie; nothing here sends a userId.
 *
 * Usage:
 *     await NotificationService.init();        // load config + SDK
 *     await NotificationService.signIn(id, pass);
 *     await NotificationService.enable();      // permission + token + register
 * ──────────────────────────────────────────────────────────────────────────── */

const FIREBASE_SDK = 'https://www.gstatic.com/firebasejs/10.12.2';

const NotificationService = (() => {
  let cfg = null;              // { firebase:{...}, vapidKey, ready, senderReady }
  let messaging = null;
  let swReg = null;
  let currentToken = null;
  let onMessageCb = null;

  const listeners = [];
  const emit = (evt, detail) => listeners.forEach(fn => fn(evt, detail));

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${src}"]`)) return resolve();
      const s = document.createElement('script');
      s.src = src; s.onload = resolve; s.onerror = () => reject(new Error(`failed to load ${src}`));
      document.head.appendChild(s);
    });
  }

  function browserName() {
    const ua = navigator.userAgent;
    if (/Edg\//.test(ua)) return 'Edge';
    if (/OPR\//.test(ua)) return 'Opera';
    if (/Chrome\//.test(ua)) return 'Chrome';
    if (/Firefox\//.test(ua)) return 'Firefox';
    if (/Safari\//.test(ua)) return 'Safari';
    return 'Browser';
  }

  /* A stable per-browser id, so the admin can tell two Chromes apart. */
  function deviceId() {
    let id = localStorage.getItem('trinetra-device-id');
    if (!id) {
      id = 'dev_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
      localStorage.setItem('trinetra-device-id', id);
    }
    return id;
  }

  function supported() {
    return 'serviceWorker' in navigator && 'Notification' in window && 'PushManager' in window;
  }

  async function init() {
    if (cfg) return cfg;
    cfg = await (await fetch('/api/notifications/config')).json();
    return cfg;
  }

  async function loadSDK() {
    await loadScript(`${FIREBASE_SDK}/firebase-app-compat.js`);
    await loadScript(`${FIREBASE_SDK}/firebase-messaging-compat.js`);
    if (!firebase.apps.length) firebase.initializeApp(cfg.firebase);
    messaging = firebase.messaging();
  }

  /* The SW gets the config via query string rather than hardcoding it twice. */
  async function registerSW() {
    if (swReg) return swReg;
    const qs = new URLSearchParams(cfg.firebase).toString();
    swReg = await navigator.serviceWorker.register(`/firebase-messaging-sw.js?${qs}`);
    await navigator.serviceWorker.ready;
    return swReg;
  }

  async function me() {
    return await (await fetch('/api/notifications/me', { credentials: 'same-origin' })).json();
  }

  async function signIn(userId, passcode) {
    const r = await fetch('/api/notifications/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ userId, passcode }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || 'Sign-in failed');
    }
    return await r.json();
  }

  /* Full opt-in flow. Only callable once signed in — enforced server-side too. */
  async function enable() {
    if (!supported()) throw new Error('This browser does not support web push.');
    await init();
    if (!cfg.ready) {
      throw new Error('Push is not configured on the server yet (missing VAPID key).');
    }

    const who = await me();
    if (!who.signedIn) throw new Error('Sign in with your user ID first.');

    const permission = await Notification.requestPermission();
    emit('permission', permission);
    if (permission !== 'granted') {
      throw new Error(permission === 'denied'
        ? 'Notifications are blocked for this site. Enable them in your browser settings.'
        : 'Notification permission was dismissed.');
    }

    await loadSDK();
    await registerSW();

    const token = await messaging.getToken({
      vapidKey: cfg.vapidKey,
      serviceWorkerRegistration: swReg,
    });
    if (!token) throw new Error('Could not obtain an FCM token.');
    currentToken = token;

    await sendToken(token);
    listenForeground();
    emit('registered', { token, userId: who.userId });
    return { userId: who.userId, token };
  }

  async function sendToken(token) {
    const r = await fetch('/api/notifications/register-device', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      // NOTE: no userId — the backend takes it from the session cookie.
      body: JSON.stringify({
        fcmToken: token,
        platform: 'WEB',
        browser: browserName(),
        deviceId: deviceId(),
      }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || 'Could not register this device');
    }
    localStorage.setItem('trinetra-fcm-token', token);
    return await r.json();
  }

  /* Foreground messages don't raise an OS notification by themselves. */
  function listenForeground() {
    if (!messaging) return;
    messaging.onMessage((payload) => {
      emit('message', payload);
      if (onMessageCb) { onMessageCb(payload); return; }
      const n = payload.notification || {};
      const d = payload.data || {};
      if (Notification.permission === 'granted' && swReg) {
        swReg.showNotification(n.title || 'Trinetra', {
          body: n.body || '',
          icon: '/assets/Trinetra-Logo-Full-Dark.png',
          tag: d.eventId || undefined,
          data: d,
        });
      }
    });
  }

  /* FCM can rotate a token; re-registering keeps the backend in step. */
  async function refresh() {
    if (!messaging || !cfg) return null;
    try {
      const token = await messaging.getToken({
        vapidKey: cfg.vapidKey, serviceWorkerRegistration: swReg,
      });
      if (token && token !== currentToken) {
        currentToken = token;
        await sendToken(token);
        emit('refreshed', { token });
      }
      return token;
    } catch (e) {
      console.warn('[push] token refresh failed:', e.message);
      return null;
    }
  }

  /* Detaching the token on sign-out is what stops the next person who signs in
     on this browser from receiving the previous user's notifications. The
     backend also re-points the token on the next register, so this is
     belt-and-braces. */
  async function signOut() {
    const token = currentToken || localStorage.getItem('trinetra-fcm-token');
    try {
      await fetch('/api/notifications/logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ fcmToken: token || '' }),
      });
    } finally {
      try { if (messaging) await messaging.deleteToken(); } catch (_) { }
      localStorage.removeItem('trinetra-fcm-token');
      currentToken = null;
      emit('signedout', {});
    }
  }

  return {
    init, me, signIn, enable, refresh, signOut, supported, browserName, deviceId,
    onMessage: (fn) => { onMessageCb = fn; },
    on: (fn) => listeners.push(fn),
    get token() { return currentToken; },
    get config() { return cfg; },
  };
})();

window.NotificationService = NotificationService;
