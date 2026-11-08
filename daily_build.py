#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 每交易日收盘后自动选股执行器。

背景：build_site.py 会用旧版模板重新生成 index.html，覆盖线上已经打补丁的页面
（手动刷新、四级建议、六维评分、三日量能、normCode 现价修复、持仓卡今日盈亏）。
因此本脚本的做法是：
1) 先跑 build_site.py，拿到最新的 data.json / candidates.json；
2) 跑完把 index_patched.html（线上已修版页面）还原成 index.html，保证前端功能不被回退。
"""
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
PATCHED = BASE / "index_patched.html"


def main() -> int:
    if not PATCHED.exists():
        print("缺少 index_patched.html，中止，避免用旧模板覆盖线上页面", flush=True)
        return 2

    print("[1/3] 运行 build_site.py 抓取行情并生成 data.json / candidates.json ...", flush=True)
    rc = subprocess.run([sys.executable, "build_site.py"], cwd=str(BASE)).returncode
    if rc != 0:
        print("build_site.py 失败，退出码 %d，不还原页面" % rc, flush=True)
        return rc

    print("[2/3] 还原已修版 index.html（不采用 build_site.py 生成的旧模板）", flush=True)
    shutil.copyfile(PATCHED, BASE / "index.html")

    data = BASE / "data.json"
    cand = BASE / "candidates.json"
    print("[3/3] 产物：data.json=%d B, candidates.json=%d B, index.html=%d B"
          % (data.stat().st_size, cand.stat().st_size, (BASE / "index.html").stat().st_size), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
