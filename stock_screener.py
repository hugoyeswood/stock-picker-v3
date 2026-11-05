# -*- coding: utf-8 -*-
"""
【你的自动选股小助手】V1
====================================================
每天收盘后跑一次：自动拉全市场数据 -> 按规则筛选 ->
生成一份"今日值得关注的候选清单"（网页看板 + 表格）。

你现在是"半自动"：软件负责选股出信号，买卖由你在券商App手动下单。

怎么用（3秒上手）：
  1. 双击/命令行运行：  python stock_screener.py
  2. 程序自动下载数据、筛选，约半分钟
  3. 生成的文件： report_YYYY-MM-DD.html  （双击用浏览器打开，就是你的每日候选清单）
                cands_YYYY-MM-DD.csv   （表格版，可用Excel打开）

内置的"1000元买得起"规则：
  - 只挑 最新价 * 100股  <= 1000元  的股票（你资金有限，选贵的也买不起）
  - 同时自动排除 北交所、科创板、创业板(没开通权限买不了)、ST股、退市股
====================================================
"""
import akshare as ak
import pandas as pd
from datetime import datetime

# ============ 1. 你可以改的"筛股口味" ============
BUDGET = 1000.0        # 你的可用资金（元）
TOP_N  = 20            # 每天最多推给你几只
MIN_PRICE = 0.5        # 太便宜(低于0.5元)的多为风险股，排除
MAX_PRICE = 10.0       # 保证 1000元 买得起一手(100股)

# ============ 2. 拉全市场数据（腾讯源，免费） ============
print("正在下载全市场行情（约半分钟）...")
df = ak.stock_zh_a_spot_tx()

# 字段改名，方便看懂
df = df.rename(columns={
    "zxj": "现价", "zdf": "今日涨跌%", "zdf_d5": "5日涨跌%",
    "zdf_d10": "10日涨跌%", "zdf_d20": "20日涨跌%", "zdf_d60": "60日涨跌%",
    "lb": "量比", "hsl": "换手率%", "ltsz": "流通市值亿", "zsz": "总市值亿",
    "pe_ttm": "市盈率TTM", "zljlr": "主力净流入万", "name": "名称",
    "code": "代码", "stock_type": "板块",
})
print(f"已拿到全市场 {len(df)} 只股票")

# 腾讯接口把数字当成文字（停牌股会返回"--"），这里统一转成数字，转不了的空着
num_cols = ["现价", "今日涨跌%", "5日涨跌%", "10日涨跌%", "20日涨跌%", "60日涨跌%",
            "量比", "换手率%", "市盈率TTM", "主力净流入万", "流通市值亿", "总市值亿"]
for c in num_cols:
    df[c] = pd.to_numeric(df[c], errors="coerce")

# ============ 3. 先做"能不能买"的过滤（资金约束优先） ============
def 板块_可买(code):
    """只留沪深主板：6开头(沪) / 0开头(深)。排除 688科创板、300创业板、bj北交所"""
    if code.startswith("sh6"):
        return True
    if code.startswith(("sz000", "sz001", "sz002", "sz003")):
        return True
    return False

mask = (
    df["代码"].apply(板块_可买)                      # 主板
    & (~df["名称"].str.contains("ST", na=False))      # 排除ST
    & (df["现价"] >= MIN_PRICE)
    & (df["现价"] <= MAX_PRICE)                       # 1000元买得起
    & (df["量比"].fillna(0) > 0)
)
pool = df[mask].copy()
print(f"过滤后，1000元买得起的沪深主板候选 {len(pool)} 只")

# ============ 4. 打分：简单好懂的"趋势+放量"信号 ============
def 打分(r):
    s = 0
    reasons = []
    # 今日放量上涨（量比>1.2 且 上涨超过1%）
    if r["今日涨跌%"] > 1.0 and r["量比"] > 1.2:
        s += 2; reasons.append("今日放量上涨")
    elif r["今日涨跌%"] > 0:
        s += 1; reasons.append("今日上涨")
    # 中期趋势向上（近10日、20日都涨）
    if r["10日涨跌%"] > 0:
        s += 1; reasons.append("近10日走强")
    if r["20日涨跌%"] > 0:
        s += 1; reasons.append("近20日走强")
    # 主力资金净流入
    if r["主力净流入万"] > 0:
        s += 1; reasons.append("主力资金流入")
    # 换手活跃（1%~10% 之间，太冷或太热都不好）
    if 1 <= r["换手率%"] <= 10:
        s += 1; reasons.append("换手活跃")
    # 估值适中（市盈率 0~60 之间，太高或亏损先不看）
    if 0 < r["市盈率TTM"] < 60:
        s += 1; reasons.append("估值适中")
    return s, "、".join(reasons)

pool["信号分"] = 0
pool["理由"] = ""
rows = pool.to_dict("records")
scored = []
for r in rows:
    s, why = 打分(r)
    if s >= 3:                      # 至少3分才值得看，过滤噪音
        scored.append((s, r, why))

scored.sort(reverse=True, key=lambda x: x[0])
top = scored[:TOP_N]
print(f"达到信号门槛(≥3分)的有 {len(scored)} 只，取前 {TOP_N} 只")

# ============ 5. 组装结果 ============
out = []
for s, r, why in top:
    price = r["现价"]
    hands = int(BUDGET // (price * 100))          # 1000元能买几手(一手=100股)
    out.append({
        "代码": r["代码"], "名称": r["名称"], "现价": price,
        "今日涨跌%": round(r["今日涨跌%"], 2), "量比": round(r["量比"], 2),
        "换手率%": round(r["换手率%"], 2), "市盈率TTM": round(r["市盈率TTM"], 2),
        "5日涨跌%": round(r["5日涨跌%"], 2), "10日涨跌%": round(r["10日涨跌%"], 2),
        "20日涨跌%": round(r["20日涨跌%"], 2),
        "信号分": s, "理由": why,
        "1000元可买": f"{hands}手(约{hands*price*100:.0f}元)",
    })

res = pd.DataFrame(out)
today = datetime.now().strftime("%Y-%m-%d")
res.to_csv(f"cands_{today}.csv", index=False, encoding="utf-8-sig")

# ============ 6. 生成网页看板（双击就能看） ============
def css_color(v):
    return "#d32f2f" if v > 0 else ("#2e7d32" if v < 0 else "#666")

rows_html = ""
for _, r in res.iterrows():
    c1 = css_color(r["今日涨跌%"])
    c5 = css_color(r["5日涨跌%"]); c10 = css_color(r["10日涨跌%"]); c20 = css_color(r["20日涨跌%"])
    rows_html += f"""
    <tr>
      <td class="mono">{r['代码']}</td>
      <td>{r['名称']}</td>
      <td class="mono">{r['现价']:.2f}</td>
      <td class="mono" style="color:{c1}">{r['今日涨跌%']:+.2f}%</td>
      <td class="mono" style="color:{c5}">{r['5日涨跌%']:+.2f}%</td>
      <td class="mono" style="color:{c10}">{r['10日涨跌%']:+.2f}%</td>
      <td class="mono" style="color:{c20}">{r['20日涨跌%']:+.2f}%</td>
      <td class="mono">{r['量比']:.2f}</td>
      <td class="mono">{r['换手率%']:.2f}%</td>
      <td class="mono">{r['市盈率TTM']:.2f}</td>
      <td><span class="score">{r['信号分']}</span></td>
      <td class="why">{r['理由']}</td>
      <td class="mono">{r['1000元可买']}</td>
    </tr>"""

html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>今日候选清单 {today}</title>
<style>
  body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 24px; background:#f5f6f8; }}
  h1 {{ color: #1a237e; }} .sub {{ color:#666; margin-bottom:16px; }}
  table {{ border-collapse: collapse; width:100%; background:#fff; box-shadow:0 1px 6px rgba(0,0,0,.08); }}
  th, td {{ border:1px solid #e0e0e0; padding:8px 10px; font-size:14px; }}
  th {{ background:#1a237e; color:#fff; }}
  tr:nth-child(even) {{ background:#fafafa; }}
  .mono {{ font-family: Consolas, monospace; }}
  .score {{ font-weight:bold; color:#c62828; }}
  .why {{ color:#444; max-width:220px; }}
  .tip {{ background:#fff8e1; border-left:4px solid #f9a825; padding:12px 16px; margin:16px 0; }}
</style></head><body>
<h1>今日候选清单（{today}）</h1>
<div class="sub">自动选股 · 免费数据 · 半自动（信号参考，下单请自己确认）&nbsp;|&nbsp;共 {len(res)} 只</div>
<div class="tip"><b>怎么用：</b>只看信号分高的几只，打开你的券商App，按"现价"手动买入1手（100股）。<b>重要提醒：</b>这不是买卖建议，只是数据筛选；涨跌有风险，务必自己判断、控制仓位。</div>
<table>
<tr><th>代码</th><th>名称</th><th>现价</th><th>今日</th><th>5日</th><th>10日</th><th>20日</th>
<th>量比</th><th>换手</th><th>市盈率</th><th>信号分</th><th>入选理由</th><th>1000元可买</th></tr>
{rows_html}
</table></body></html>"""

with open(f"report_{today}.html", "w", encoding="utf-8") as f:
    f.write(html)

print("\n完成！生成了：")
print(f"  网页看板 -> report_{today}.html  （双击用浏览器打开）")
print(f"  表格数据 -> cands_{today}.csv   （可用Excel打开）")
