/* Service worker for Stekkies Agent web-push notifications.
   Served from / (see app.py) so its scope covers the whole dashboard. */

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { /* text push */ }
  const title = data.title || 'Stekkies Agent';
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || '',
    tag: data.tag || 'stekkies',
    data: { url: data.url || '/' },
    // Re-alert even when a notification with the same tag is replaced —
    // a second submission on the same source should still buzz the phone.
    renotify: true,
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true })
    .then((wins) => {
      for (const w of wins) {
        if ('focus' in w) return w.focus();
      }
      return clients.openWindow(url);
    }));
});
