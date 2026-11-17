/* 离线缓存：HTML/JSON 永不落缓存，静态资源 network-first 兜底 */
var VER = '20261117n53';
var CACHE = 'stock-picker-v3-' + VER;
var STATIC = [
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png'
];
/* 这些路径一律走纯网络（no-store），失败才回退缓存，避免旧页面被长期顶住 */
var NET_ONLY = /(index\.html|index_patched\.html|short_term_rec\.json|data\.json|candidates\.json|version\.json|manifest\.json|service-worker\.js)(\?|$)/;

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(CACHE).then(function (c) { return c.addAll(STATIC).catch(function () {}); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('message', function (e) {
  if (e.data === 'SKIP_WAITING') self.skipWaiting();
  if (e.data === 'PURGE') {
    e.waitUntil(caches.keys().then(function (ks) {
      return Promise.all(ks.map(function (k) { return caches.delete(k); }));
    }));
  }
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  var isOwn = url.origin === self.location.origin;

  // 页面导航与动态数据：永不缓存优先，只在彻底断网时兜底
  if (req.mode === 'navigate' || (isOwn && NET_ONLY.test(url.pathname))) {
    e.respondWith(
      fetch(req, { cache: 'no-store' })
        .catch(function () {
          return caches.match(req).then(function (m) { return m || caches.match('./index.html'); });
        })
    );
    return;
  }

  // 其余同源静态资源：network-first，成功才写缓存
  e.respondWith(
    fetch(req, isOwn ? { cache: 'no-store' } : undefined)
      .then(function (res) {
        if (isOwn && res && res.status === 200) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(req, copy); }).catch(function () {});
        }
        return res;
      })
      .catch(function () {
        return caches.match(req).then(function (m) { return m || caches.match('./index.html'); });
      })
  );
});

// 点通知 → 打开/聚焦站点
self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) || './';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if ('focus' in c) { if (c.navigate) c.navigate(url).catch(function () {}); return c.focus(); }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
