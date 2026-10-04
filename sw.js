const CACHE = 'vko-v4';

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(['/'])).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API immer live – nie cachen
  if (url.pathname.startsWith('/api/')) return;

  // Icons + Manifest immer live – nie cachen
  if (/\.(png|ico|svg)$/.test(url.pathname) || url.pathname.includes('manifest')) return;

  // Nur die öffentliche App-Shell "/" cachen (Network-first, Cache-Fallback offline).
  // Vereinsbereich und /admin nie: deren Seiten enthalten E-Mail-Adressen und blieben
  // sonst nach dem Logout im Gerät liegen. Fehlerseiten (Wartung 503 usw.) nicht cachen.
  if (e.request.method === 'GET' && url.pathname === '/') {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          if (res.ok) {
            const kopie = res.clone();
            caches.open(CACHE).then(c => c.put('/', kopie));
          }
          return res;
        })
        .catch(() => caches.match('/'))
    );
    return;
  }
});
