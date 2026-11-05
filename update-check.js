/* 版本自检：当本地页面是旧缓存（WebView/PWA 缓存）时，自动跳转到线上最新版本 */
(function () {
  var script = document.currentScript || (function () {
    var all = document.getElementsByTagName('script');
    return all[all.length - 1];
  })();
  var pageUpd = '';
  try { pageUpd = (script && script.getAttribute('data-page-upd')) || ''; } catch (e) {}
  if (!pageUpd) return;

  var fired = false;
  function jump() {
    if (fired) return;
    fired = true;
    try {
      var u = location.pathname + '?fresh=' + new Date().getTime();
      window.location.replace(u);
    } catch (e) {}
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
          if (d && d.updated && String(d.updated) !== String(pageUpd)) jump();
        } catch (e) {}
      };
      x.send();
    } catch (e) {}
  }
  setTimeout(check, 2500);
  setInterval(check, 60000);
})();
