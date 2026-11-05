# -*- coding: utf-8 -*-
"""校验"模拟云端发布"后的站点：页面引用的本地资源是否都在"""
import re, os, sys

d = sys.argv[1]
s = open(os.path.join(d, "index.html"), encoding="utf-8").read()
refs = set(re.findall(r'(?:href|src)="([^"]+)"', s))
refs = {r for r in refs if not r.startswith(("http", "data")) and r != "#"}
missing = [r for r in refs if not os.path.exists(os.path.join(d, r))]

print("站点文件清单:", sorted(os.listdir(d)))
print("icons 目录:", sorted(os.listdir(os.path.join(d, "icons"))))
print("页面引用的本地资源缺失:", missing if missing else "无 ✓")
