/* ────────────────────────────────────────────────────────────────────────────
 * Firebase Messaging service worker — handles BACKGROUND notifications
 * (tab closed, or another tab focused). Foreground messages are handled by
 * js/notifications.js instead.
 *
 * Must be served from the site ROOT (/firebase-messaging-sw.js) so its scope
 * covers every page. FastAPI already serves frontend/ at /, so this file is
 * reachable there with no extra routing.
 *
 * The Firebase config arrives as query params from the registration call in
 * js/notifications.js, so it is never duplicated/hardcoded here.
 * ──────────────────────────────────────────────────────────────────────────── */

importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js');

const params = new URL(self.location).searchParams;
const cfg = {
  apiKey: params.get('apiKey'),
  authDomain: params.get('authDomain'),
  projectId: params.get('projectId'),
  storageBucket: params.get('storageBucket'),
  messagingSenderId: params.get('messagingSenderId'),
  appId: params.get('appId'),
};

if (cfg.apiKey && cfg.messagingSenderId) {
  firebase.initializeApp(cfg);
  const messaging = firebase.messaging();

  messaging.onBackgroundMessage((payload) => {
    const n = payload.notification || {};
    const d = payload.data || {};
    self.registration.showNotification(n.title || 'Trinetra', {
      body: n.body || '',
      icon: '/assets/Trinetra-Logo-Full-Dark.png',
      badge: '/assets/Trinetra-Logo-Full-Dark.png',
      tag: d.eventId || undefined,      // collapses repeats for the same event
      data: d,
    });
  });
}

/* Clicking a notification focuses an existing dashboard tab if one is open,
   otherwise opens a new one — then navigates to the event's page. */
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const d = event.notification.data || {};
  const target = d.link || (d.sessionId ? `/visits.html?visit=${d.sessionId}` : '/index.html');

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ('focus' in client) {
          client.navigate(target);
          return client.focus();
        }
      }
      return clients.openWindow(target);
    })
  );
});
