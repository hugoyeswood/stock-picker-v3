/* 版本自检：本地页面是旧缓存（WebView/PWA 缓存）时自动切到线上最新版本。
   修复点：原实现拿 data.json 的 updated 与页面写死的 2026-11-06 07:40 比较，
   必然不相等 → 每 2.5 秒 location.replace 一次，页面陷入自动刷新循环。
   现在：页面标记由构建脚本按 data.json 的 updated 注入；并加 10 分钟一次的跳转闸门。 */
(function () {
  var script = document.currentScript || (function () {
    var all = document.getElementsByTagName('script');
    return all[all.length - 1];
  })();
  var pageUpd = '';
  try { pageUpd = (script && script.getAttribute('data-page-upd')) || ''; } catch (e) {}
  if (!pageUpd) return;

  var GUARD_KEY = 'updJumpTs';
  var fired = false;

  function cooled() {
    try {
      var last = +(sessionStorage.getItem(GUARD_KEY) || 0);
      return (new Date().getTime() - last) < 10 * 60 * 1000;
    } catch (e) { return false; }
  }

  function jump() {
    if (fired || cooled()) return;
    fired = true;
    try { sessionStorage.setItem(GUARD_KEY, String(new Date().getTime())); } catch (e) {}
    try {
      var u = location.pathname + '?fresh=' + new Date().getTime();
      window.location.replace(u);
    } catch (e) {}
  }

  function same(a, b) {
    return String(a || '').trim().slice(0, 16) === String(b || '').trim().slice(0, 16);
  }

  function check() {
    try {
      var x = new XMLHttpRequest();
      x.open('GET', './data.json?ts=' + new Date().getTime(), true);
      x.timeout = 15000;
      x.onload = function () {
        if (x.status !== 200) return;
        try {
          var d = JSON.parse(x.responseText);
          if (!d || !d.updated) return;
          window.__dataUpdated = d.updated;
          if (typeof refreshHeadMeta === 'function') refreshHeadMeta();
          if (!same(d.updated, pageUpd)) jump();
        } catch (e) {}
      };
      x.send();
    } catch (e) {}
  }

  setTimeout(check, 3000);
  setInterval(check, 120000);
})();
