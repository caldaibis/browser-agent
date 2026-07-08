// Dashboard client behavior. Extracted from base.html.

// --- theme toggle (persists to localStorage; default follows OS) ---
(function () {
  const saved = localStorage.getItem('theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  window.toggleTheme = function () {
    const cur = document.documentElement.getAttribute('data-theme');
    const isDark = cur === 'dark' ||
      (!cur && window.matchMedia('(prefers-color-scheme: dark)').matches);
    const next = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    document.dispatchEvent(new Event('themechange'));
  };
})();

// --- rows tagged data-href navigate on click (unless a nested link/button) ---
document.addEventListener('click', (e) => {
  const row = e.target.closest('tr[data-href]');
  if (!row || e.target.closest('a, button')) return;
  window.location.href = row.dataset.href;
});

// --- chart palette from CSS variables, so charts match the active theme ---
window.chartPalette = function () {
  const s = getComputedStyle(document.documentElement);
  return [1, 2, 3, 4, 5, 6, 7, 8].map((i) => s.getPropertyValue('--c' + i).trim());
};

// --- web push: native notifications for submissions ---
(async () => {
  const btn = document.getElementById('push-toggle');
  if (!btn || !('serviceWorker' in navigator) || !('PushManager' in window)) return;
  btn.hidden = false;

  const b64ToBytes = (b64) => {
    const pad = '='.repeat((4 - b64.length % 4) % 4);
    const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  };
  const reg = await navigator.serviceWorker.register('/sw.js');
  const current = async () => (await reg.pushManager.getSubscription());
  const render = async () => {
    btn.textContent = (await current()) ? '🔔 Notifications on' : '🔕 Enable notifications';
  };
  await render();

  btn.addEventListener('click', async () => {
    try {
      const existing = await current();
      if (existing) {
        await fetch('/push/unsubscribe', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: existing.endpoint }),
        });
        await existing.unsubscribe();
      } else {
        if (await Notification.requestPermission() !== 'granted') return;
        const { key } = await (await fetch('/push/public-key')).json();
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: b64ToBytes(key),
        });
        const r = await fetch('/push/subscribe', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(sub.toJSON()),
        });
        if (!r.ok) { await sub.unsubscribe(); alert('Subscribe failed'); }
      }
    } catch (e) {
      alert('Push setup failed: ' + e);
    }
    await render();
  });
})();
