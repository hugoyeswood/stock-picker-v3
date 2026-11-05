# -*- coding: utf-8 -*-
"""前端稳定性检查：HTML结构 / 资源存在性 / JSON合法性 / 转义覆盖"""
import json, os
from html.parser import HTMLParser

results = []

# 1) HTML 结构
class P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []
    def handle_starttag(self, tag, attrs):
        if tag not in ("br", "img", "meta", "link", "input", "hr", "source"):
            self.stack.append(tag)
    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append(f"</{tag}> 不匹配 (栈顶={self.stack[-1] if self.stack else None})")

html_src = open("index.html", encoding="utf-8").read()
p = P(); p.feed(html_src)
results.append(("HTML 未闭合标签", "无 ✓" if not p.stack else str(p.stack)))
results.append(("HTML 标签匹配错误", "无 ✓" if not p.errors else str(p.errors)))
results.append(("viewport 移动端适配", "有 ✓" if 'name="viewport"' in html_src else "缺失 ✗"))
results.append(("manifest 链接", "有 ✓" if 'rel="manifest"' in html_src else "缺失 ✗"))

# 2) 引用的资源是否存在
for f in ["manifest.json", "service-worker.js",
          "icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png"]:
    results.append((f, "存在 ✓" if os.path.exists(f) else "缺失 ✗"))

# 3) manifest JSON
try:
    m = json.load(open("manifest.json", encoding="utf-8"))
    sizes = [i.get("sizes") for i in m.get("icons", [])]
    results.append(("manifest.json 合法", "✓ " + m.get("name", "") + " / display=" + m.get("display", "") + " / icons=" + str(sizes)))
except Exception as e:
    results.append(("manifest.json 合法", "✗ " + str(e)))

# 4) data.json JSON
try:
    d = json.load(open("data.json", encoding="utf-8"))
    results.append(("data.json 合法", "✓ count=" + str(d.get("count")) + " budget=" + str(d.get("budget"))))
except Exception as e:
    results.append(("data.json 合法", "✗ " + str(e)))

# 5) service-worker.js 花括号配对
sw = open("service-worker.js", encoding="utf-8").read()
results.append(("service-worker 花括号", "✓" if sw.count("{") == sw.count("}") else "✗ 不配对"))

# 6) build_site.py 动态文本转义覆盖
bs = open("build_site.py", encoding="utf-8").read()
checks = {
    "定义 esc()": "def esc(" in bs,
    "转义 name": "esc(r['name'])" in bs,
    "转义 code": "esc(r['code'])" in bs,
    "转义 advice": "esc(t['advice'])" in bs,
    "转义 state": "esc(t['state'])" in bs,
    "转义 confirm": "esc(t['confirm'])" in bs,
    "转义 risk": 'esc(t["risk_note"])' in bs,
}
for k, v in checks.items():
    results.append((k, "✓" if v else "✗"))

for name, ok in results:
    print(f"[{ok}] {name}")
