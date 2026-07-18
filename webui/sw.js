/* Achievement Box service worker: makes the app installable and shows
   push notifications for unlocks even when the app is closed. Network
   passthrough for fetches (the server already sets sane cache headers;
   the box is on the LAN so "offline" mostly means "box off"). */

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));

self.addEventListener('push', e => {
  let data = {};
  try { data = e.data.json(); } catch { /* fall through */ }
  const title = data.title || 'Achievement unlocked!';
  e.waitUntil(self.registration.showNotification(title, {
    body: data.body || '',
    icon: data.icon || '/icon-192.png',
    // status-bar icon: must be a monochrome alpha mask or Android
    // flattens it to a white square
    badge: '/badge-96.png',
    vibrate: [90, 40, 120],
    tag: data.tag || 'achievementbox',
    renotify: true,
    data: {url: '/'}
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true})
    .then(list => {
      for (const c of list) if ('focus' in c) return c.focus();
      return clients.openWindow('/');
    }));
});
