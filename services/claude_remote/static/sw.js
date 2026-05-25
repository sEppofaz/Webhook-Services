const CACHE = "claude-remote-v1";
const SHELL = [
  "/claude-remote/",
  "/claude-remote/manifest.json",
  "/claude-remote/icon-192.png",
  "/claude-remote/icon-512.png",
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // API-Calls nie cachen
  if (url.pathname.startsWith("/api/")) return;
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
