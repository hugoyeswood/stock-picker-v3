/* 离线缓存：优先取最新（每天数据会变），断网时用上次缓存的 */
const CACHE = 'stock-picker-v3-20261113w';
const ASSETS = [
  './', './index.html', './data.json', './manifest.json', './update-check.js',
  './icons/icon-192.png', './icons/icon-512.png', './icons/apple-touch-icon.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// network-first：在线时永远拿最新，离线时用缓存兜底
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const isOwn = e.request.url.indexOf(self.location.origin) === 0;
  e.respondWith(
    fetch(e.request, isOwn ? { cache: 'no-store' } : undefined)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() =>
        caches.match(e.request).then((m) => m || caches.match('./index.html'))
      )
  );
});
