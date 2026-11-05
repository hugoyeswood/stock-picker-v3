# -*- coding: utf-8 -*-
"""
【你的自动选股小助手】手机版 + 云端版 + 买卖点 + 资金账户  build_site.py
====================================================
每天自动：拉全市场数据 -> 筛选 -> 为候选算买卖点 -> 生成手机页面 index.html + 数据 data.json

功能：
  1. 资金账户：从 wallet.json 读取"当前可用资金"，按最新资金筛选"买得起的股"；
     想加钱用 add_fund.py（资金转入端口）。
  2. 买卖点：每只候选给出 该买/等回踩/观望 + 买入参考价/止损/止盈 +
     建议买入区间 + 追高风险提示 + 买入前确认清单。
  3. 安全：所有写入页面的文字都会做转义，防止网页注入问题。
     本工具不保存任何账号密码，只读公开免费行情。

数据源自动切换：腾讯 -> 东方财富 -> 新浪。
====================================================
"""
import akshare as ak
import pandas as pd
import json
import html
from datetime import datetime, timedelta

# ============ 1. 可调的"选股口味" ============
BUDGET_DEFAULT = 1000.0   # 没有资金账户文件时用的默认资金
TOP_N    = 20             # 每天最多推几只
MIN_PRICE = 0.5
MAX_PRICE = 10.0

# ============ 2. 读取资金账户 ============
def load_wallet():
    """从 wallet.json 读当前可用资金；文件不存在或出错时用默认值"""
    try:
        with open("wallet.json", encoding="utf-8") as f:
            w = json.load(f)
        return float(w.get("available", BUDGET_DEFAULT)), w.get("ledger", [])
    except Exception:
        return BUDGET_DEFAULT, []

WALLET_AMT, WALLET_LEDGER = load_wallet()
BUDGET = WALLET_AMT

# ============ 3. 多数据源自动切换 ============
def normalize(df, source):
    if source == "腾讯":
        ren = {"zxj":"price","zdf":"chg","zdf_d5":"chg5","zdf_d10":"chg10",
               "zdf_d20":"chg20","zdf_d60":"chg60","lb":"lb","hsl":"hsl",
               "pe_ttm":"pe","zljlr":"mflow","ltsz":"ltsz","stock_type":"stype"}
    elif source == "东财":
        ren = {"最新价":"price","涨跌幅":"chg","量比":"lb","换手率":"hsl",
               "市盈率-动态":"pe","总市值":"ltsz","股票代码":"code","股票简称":"name"}
    else:
        ren = {"最新价":"price","涨跌幅":"chg"}
    df = df.rename(columns=ren)
    for col in ["chg5","chg10","chg20","chg60","lb","hsl","pe","mflow","ltsz"]:
        if col not in df.columns:
            df[col] = float("nan")
    for col in ["price","chg","chg5","chg10","chg20","chg60","lb","hsl","pe","mflow","ltsz"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["source"] = source
    return df

def fetch_all():
    for source, fn in [("腾讯", ak.stock_zh_a_spot_tx),
                       ("东财", ak.stock_zh_a_spot_em),
                       ("新浪", ak.stock_zh_a_spot)]:
        try:
            df = fn()
            print(f"[数据源] 用 {source}")
            return normalize(df, source), source
        except Exception as e:
            print(f"[数据源] {source} 失败({str(e)[:50]})，换下一个...")
    raise RuntimeError("所有数据源都失败")

# ============ 4. 算买卖点（拉历史K线） ============
def calc_trade(code, price):
    """拉近250天历史，算均线/支撑，给出买卖点。失败返回带兜底信息的字典"""
    fallback = {"state": "未知", "advice": "历史数据暂缺，建议先看该股K线再决定",
                "buy_ref": round(price, 2), "stop_loss": None, "take_profit": None,
                "ma5": None, "ma20": None, "low20": None,
                "buy_low": round(price, 2), "buy_high": round(price, 2),
                "risk_note": "", "confirm": "历史数据暂缺，买入前请人工确认趋势",
                "kline30": []}
    try:
        end = datetime.now()
        start = end - timedelta(days=250)
        hist = ak.stock_zh_a_hist_tx(symbol=code,
                                     start_date=start.strftime("%Y%m%d"),
                                     end_date=end.strftime("%Y%m%d"),
                                     adjust="")
        if hist is None or hist.empty or price is None:
            return fallback
        close = pd.to_numeric(hist["close"], errors="coerce")
        low   = pd.to_numeric(hist["low"],  errors="coerce")
        high  = pd.to_numeric(hist["high"], errors="coerce")
        ma5  = close.tail(5).mean()
        ma20 = close.tail(20).mean()
        low20 = low.tail(20).min()
        if pd.isna(ma20):
            return fallback
        # 状态
        if price >= ma20:
            dist = (price - ma5) / ma5 * 100 if (pd.notna(ma5) and ma5 > 0) else 0
            if dist <= 5:
                state = "可买"; advice = "趋势向上，可按参考区间买入1手"
            else:
                state = "等回踩"; advice = f"短线涨得急，等回踩到MA5(约{ma5:.2f}元)附近再买更稳"
        else:
            state = "观望"; advice = "现价在20日线下方，先观察，别急着追"
        # 建议买入区间：现价与MA5的较低者 ~ 现价（别高于现价追）
        buy_low = round(min(price, ma5), 2) if pd.notna(ma5) else round(price, 2)
        buy_high = round(price, 2)
        stop = round(price * 0.95, 2)
        take = round(price * 1.10, 2)
        # 风险提示
        risks = []
        up_from_low = (price - low20) / low20 * 100 if (pd.notna(low20) and low20 > 0) else 0
        if up_from_low > 30:
            risks.append(f"较近20日低点已涨{up_from_low:.0f}%，追高风险较大")
        above_ma20 = (price / ma20 - 1) * 100 if ma20 > 0 else 0
        if above_ma20 > 12:
            risks.append(f"现价已高出20日均线约{above_ma20:.0f}%，短线偏热")
        if stop and low20 and stop < low20:
            risks.append(f"止损价{stop}已低于近20日低点{low20:.2f}，注意破位风险")
        risk_note = "；".join(risks)
        # 买入前确认清单
        confirm = (f"买前确认：①资金够买1手 ②券商挂限价单，填在{buy_low}~{buy_high}元区间 "
                   f"③同时设好止损单{stop}元 ④每次只买1手、不满仓")
        # 提取近30日K线（[date,open,close,high,low]）供前端画图
        kline30 = []
        try:
            for _, row in hist.tail(30).iterrows():
                kline30.append([str(row.get("date", ""))[:10],
                                round(float(row["open"]), 2), round(float(row["close"]), 2),
                                round(float(row["high"]), 2), round(float(row["low"]), 2)])
        except Exception:
            kline30 = []
        return {
            "state": state, "advice": advice,
            "buy_ref": round(price, 2), "stop_loss": stop, "take_profit": take,
            "ma5": round(ma5, 2) if pd.notna(ma5) else None,
            "ma20": round(ma20, 2), "low20": round(low20, 2),
            "buy_low": buy_low, "buy_high": buy_high,
            "risk_note": risk_note, "confirm": confirm,
            "kline30": kline30,
        }
    except Exception:
        return fallback

print("正在拉取全市场行情（多数据源自动切换）...")
df, src = fetch_all()
print(f"已拿到全市场 {len(df)} 只股票（数据源:{src}，可用资金:¥{BUDGET:.2f}）")

# ============ 5. "能不能买"的过滤（资金约束优先） ============
def 板块_可买(code):
    code = str(code)
    if code.startswith("sh6"): return True
    if code.startswith(("sz000","sz001","sz002","sz003")): return True
    return False

mask = (
    df["code"].apply(板块_可买)
    & (~df.get("name", pd.Series(index=df.index)).fillna("").astype(str).str.contains("ST", na=False))
    & (df["price"] >= MIN_PRICE)
    & (df["price"] <= MAX_PRICE)
    & (df["lb"].fillna(0) > 0)
)
pool = df[mask].copy()
print(f"过滤后，当前资金买得起的沪深主板候选 {len(pool)} 只")

# ============ 6. 大师打分（利弗莫尔/欧奈尔量化评分，缺数据项自动不参与） ============
# 维度对齐 DeepSeek V5 master_score：今日强势 / 放量 / 换手活跃 / 站上5日线 / PE，
# 并叠加利弗莫尔·欧奈尔操盘逻辑标签。K线维度（站上5日线）在 calc_trade 返回后补计。
def 打分(r):
    s, why = 0, []
    # —— 今日强势（利弗莫尔·趋势启动）——
    if pd.notna(r["chg"]):
        if r["chg"] >= 2.0: s += 2; why.append("今日强势上涨(利弗莫尔·趋势启动)")
        elif r["chg"] > 0: s += 1; why.append("今日上涨")
    # —— 放量确认（欧奈尔·量价齐升）——
    if pd.notna(r["lb"]):
        if r["lb"] >= 1.5: s += 2; why.append("放量确认(欧奈尔·量价齐升)")
        elif r["lb"] >= 1.0: s += 1; why.append("量比正常")
    # —— 换手活跃（3~15 活跃，>15 防过热扣分）——
    if pd.notna(r["hsl"]):
        if 3 <= r["hsl"] <= 15: s += 2; why.append("换手活跃")
        elif r["hsl"] > 15: s -= 1; why.append("换手过高防过热")
        elif r["hsl"] >= 1: s += 1; why.append("换手适中")
    # —— 中短趋势 ——
    if pd.notna(r["chg10"]) and r["chg10"] > 0: s += 1; why.append("近10日走强")
    if pd.notna(r["chg20"]) and r["chg20"] > 0: s += 1; why.append("近20日走强")
    # —— 资金与估值 ——
    if pd.notna(r["mflow"]) and r["mflow"] > 0: s += 1; why.append("主力资金流入")
    if pd.notna(r["pe"]) and 0 < r["pe"] < 80: s += 1; why.append("估值适中")
    return s, "、".join(why) if why else "综合评级"

rows = pool.to_dict("records")
scored = []
for r in rows:
    s, why = 打分(r)
    if s >= 3:
        scored.append((s, r, why))
scored.sort(reverse=True, key=lambda x: x[0])
top = scored[:TOP_N]
print(f"达到信号门槛(≥3分)的有 {len(scored)} 只，取前 {TOP_N} 只")

# ============ 7. 组装结果（含买卖点） ============
print("正在逐只拉历史K线算买卖点（约半分钟）...")
out = []
for s, r, why in top:
    price = r["price"]
    hands = int(BUDGET // (price * 100))
    trade = calc_trade(r["code"], price)
    # —— 站上5日线（K线维度，利弗莫尔·关键点 / 欧奈尔·站上均线）补计 ——
    s2 = s
    if trade.get("ma5") is not None and price > trade["ma5"]:
        s2 += 1; why2 = why + "、站上5日线(欧奈尔·上升趋势确认)"
    else:
        why2 = why + "、跌破5日线"
    # —— 大师分级：score≥7 🏆最优 · ≥5 ⭐优选 · 否则 关注（对齐 V5 lv 三档） ——
    lv = 1 if s2 >= 7 else (2 if s2 >= 5 else 3)
    out.append({
        "code": str(r["code"]), "name": str(r.get("name", "")),
        "price": round(price, 2),
        "chg": round(r["chg"], 2) if pd.notna(r["chg"]) else None,
        "chg5": round(r["chg5"], 2) if pd.notna(r["chg5"]) else None,
        "chg10": round(r["chg10"], 2) if pd.notna(r["chg10"]) else None,
        "chg20": round(r["chg20"], 2) if pd.notna(r["chg20"]) else None,
        "lb": round(r["lb"], 2) if pd.notna(r["lb"]) else None,
        "hsl": round(r["hsl"], 2) if pd.notna(r["hsl"]) else None,
        "pe": round(r["pe"], 2) if pd.notna(r["pe"]) else None,
        "score": s2, "why": why2, "lv": lv,
        "hands": hands, "cost": int(hands * price * 100),
        "trade": trade,
    })

now = datetime.now().strftime("%Y-%m-%d %H:%M")

# 保存数据备份 data.json
with open("data.json", "w", encoding="utf-8") as f:
    json.dump({"updated": now, "source": src, "budget": BUDGET,
               "count": len(out), "candidates": out}, f, ensure_ascii=False, indent=2)

# ============ 7.5 输出 V5 格式 TOP15 推荐池 candidates.json（对齐 DeepSeek V5） ============
# 字段：code/name/price/chg/score/reason/buy_low/buy_high/stop_loss/take_profit/limit_up/kline
def 涨停峰值(code, price):
    return round(price * 1.2, 2) if str(code).startswith("3") else round(price * 1.1, 2)
cand_list = []
for x in out[:15]:
    t = x["trade"]
    cand_list.append({
        "code": x["code"], "name": x["name"],
        "price": x["price"], "chg": x["chg"],
        "score": x["score"], "lv": x["lv"], "reason": x["why"],
        "buy_low": t.get("buy_low", round(x["price"], 2)),
        "buy_high": t.get("buy_high", round(x["price"], 2)),
        "stop_loss": t.get("stop_loss"), "take_profit": t.get("take_profit"),
        "limit_up": 涨停峰值(x["code"], x["price"]),
        "kline": t.get("kline30", []),
    })
cand_payload = {
    "date": datetime.now().strftime("%Y-%m-%d"),
    "updated": now,
    "source": src, "budget": BUDGET,
    "count": len(cand_list), "candidates": cand_list,
}
with open("candidates.json", "w", encoding="utf-8") as f:
    json.dump(cand_payload, f, ensure_ascii=False, indent=2)
print(f"[V5推荐池] 已生成 candidates.json TOP{cand_payload['count']}")

# ============ 8. 生成手机友好页面 index.html ============
def esc(x):
    """把所有写入页面的文字转义，防止注入/乱码"""
    return html.escape(str(x), quote=True)

def fmt_pct(v):
    return "--" if v is None else f"{v:+.2f}%"
def color(v):
    if v is None or v == 0: return "#666"
    return "#d32f2f" if v > 0 else "#2e7d32"   # A股习惯：红涨绿跌

STATE_STYLE = {"可买": ("#e8f5e9", "#2e7d32"), "等回踩": ("#fff3e0", "#e65100"),
               "观望": ("#eeeeee", "#616161"), "未知": ("#eeeeee", "#616161")}


# ============ 7.5 24小时预判（基于买卖点状态+价格位置） ============
def predict24(t):
    st = t.get("state")
    if st == "可买": return ("看多", "高", "#c62828", "#ffebee")
    elif st == "等回踩": return ("偏多震荡", "中", "#f57f17", "#fff8e1")
    else: return ("偏空震荡", "中", "#f57f17", "#fff8e1")

cards = ""
for r in out:
    t = r["trade"]
    bg, fg = STATE_STYLE.get(t["state"], STATE_STYLE["未知"])
    c_chg = color(r["chg"])
    # 24小时预判 + K线数据
    trend_label, trend_conf, trend_color, trend_bg = predict24(t)
    kline_json = ""
    try:
        k = t.get("kline30") or []
        if k: kline_json = json.dumps(k)
    except Exception:
        kline_json = ""
    chg5 = f"5日 {fmt_pct(r['chg5'])}" if r["chg5"] is not None else "5日 --"
    chg10 = f"10日 {fmt_pct(r['chg10'])}" if r["chg10"] is not None else "10日 --"
    chg20 = f"20日 {fmt_pct(r['chg20'])}" if r["chg20"] is not None else "20日 --"
    stop = f"¥{t['stop_loss']:.2f}" if t.get("stop_loss") else "--"
    take = f"¥{t['take_profit']:.2f}" if t.get("take_profit") else "--"
    risk_html = f'<div class="t-risk">⚠ {esc(t["risk_note"])}</div>' if t.get("risk_note") else ""
    # —— 大师逻辑标签（V5 精华：🏆最优/⭐优选/关注） ——
    lv_badge = "🏆 最优" if r.get("lv") == 1 else ("⭐ 优选" if r.get("lv") == 2 else "关注")
    lv_cls = "lv-1" if r.get("lv") == 1 else ("lv-2" if r.get("lv") == 2 else "lv-3")
    cards += f"""
    <div class="card">
      <div class="card-top">
        <div><div class="name">{esc(r['name'])} <span class="level-badge {lv_cls}">{lv_badge}</span></div><div class="code">{esc(r['code'])}</div></div>
        <div><span class="score">{r['score']}分</span>
             <span class="state" style="background:{bg};color:{fg}">{esc(t['state'])}</span></div>
      </div>
      <div class="price-line">
        <span class="price" data-code="{esc(r['code'])}">¥{r['price']:.2f}</span>
        <span class="chg" data-code="{esc(r['code'])}" style="color:{c_chg}">{fmt_pct(r['chg'])}</span>
      </div>
      <div class="tags">
        <span class="tag" style="color:{color(r['chg5'])}">{chg5}</span>
        <span class="tag" style="color:{color(r['chg10'])}">{chg10}</span>
        <span class="tag" style="color:{color(r['chg20'])}">{chg20}</span>
      </div>
      <div class="meta">量比 {r['lb'] if r['lb'] is not None else '--'} · 换手 {r['hsl'] if r['hsl'] is not None else '--'}% · 市盈率 {r['pe'] if r['pe'] is not None else '--'}</div>
      <div class="trade" style="background:{bg}">
        <div class="t-advice">{esc(t['advice'])}</div>
        <div class="t-row">买入参考 <b>¥{r['price']:.2f}</b> · 止损 <b>{stop}</b> · 止盈 <b>{take}</b></div>
        <div class="t-range">建议买入区间 <b>¥{t['buy_low']:.2f} ~ ¥{t['buy_high']:.2f}</b>（区间内挂单更稳，别高于现价追）</div>
        {risk_html}
        <div class="t-confirm">{esc(t['confirm'])}</div>
      </div>
      <div class="why">{esc(r['why'])}</div>
      <div class="hands">你的 ¥{BUDGET:.0f} 可买 <b>{r['hands']}手</b>（约{r['cost']}元，一手=100股）</div>
      <div class="trend-row" style="margin:8px 0;padding:8px 10px;border-radius:6px;background:{trend_bg};border-left:3px solid {trend_color};">
        <div style="font-size:13px;font-weight:600;color:{trend_color};">📊 24小时预判：{trend_label} <span style="font-size:11px;font-weight:400;color:#666;">（置信度：{trend_conf}）</span></div>
      </div>
      <button class="btn-chart" onclick="toggleChart('chart_{esc(r['code'])}')">📈 K线图</button>
      <div id="chart_{esc(r['code'])}" class="chart-container" style="display:none;">
        <div class="chart-title">近30日K线 · 红涨绿跌</div>
        <canvas id="canvas_{esc(r['code'])}" width="600" height="200"></canvas>
      </div>
      <div id="data_{esc(r['code'])}" style="display:none;">{kline_json}</div>
    </div>"""

wallet_rows = ""
for item in WALLET_LEDGER[-5:][::-1]:
    note = esc(item.get("note", "")) or "—"
    wallet_rows += f'<div class="w-row"><span class="w-date">{esc(item.get("date",""))}</span>'
    wallet_rows += f'<span class="w-amt">+¥{item["amount"]:.2f}</span><span class="w-note">{note}</span></div>'
wallet_html = f"""
<div class="card wallet">
  <div class="card-top"><div class="name">资金账户</div>
    <div class="w-total">当前可用 <b>¥{BUDGET:.2f}</b></div></div>
  <div class="w-rows">{wallet_rows}</div>
  <div class="w-hint">要转入资金？本地运行 <b>python add_fund.py</b>，然后重新部署即可。</div>
</div>"""

# ============ 7.6 今日短线推荐（读 short_term_rec.json，由每日09:00定时任务更新后重新部署） ============
short_term_html = ""
try:
    with open("short_term_rec.json", encoding="utf-8") as f:
        rec = json.load(f)
    st_date = esc(str(rec.get("date", "")))
    st_summary = esc(str(rec.get("market_summary", "")))
    st_note = esc(str(rec.get("note", "模拟交易，不构成投资建议；短线风险高，控制仓位")))
    st_cards = ""
    for s in rec.get("stocks", []):
        sc = esc(str(s.get("code",""))); sn = esc(str(s.get("name","")))
        sp = s.get("price"); sg = s.get("chg")
        sp_t = f"¥{float(sp):.2f}" if sp is not None else "--"
        sg_t = f"{float(sg):+.2f}%" if sg is not None else "--"
        scol = "#c62828" if (sg is not None and float(sg) >= 0) else "#2e7d32"
        sr = esc(str(s.get("reason","")))
        sbuy = esc(str(s.get("buy_range","--"))); sstop = esc(str(s.get("stop_loss","--"))); stake = esc(str(s.get("take_profit","--")))
        st_cards += f'<div class="st-item">'
        st_cards += f'  <div class="st-top"><b style="color:#1a237e;">{sn}</b>'
        st_cards += f'    <span class="st-badge">✓ 可买</span><span class="st-code">{sc}</span>'
        st_cards += f'    <span class="st-price price" data-code="{sc}" style="color:{scol};">{sp_t}</span> <span class="st-chg chg" data-code="{sc}">{sg_t}</span></div>'
        # 建议买入股数：优先用JSON里的qty，否则按现有资金算整数手
        st_qty = ""
        if s.get("qty"):
            st_qty = esc(str(s.get("qty")))
        else:
            try:
                if sp is not None:
                    p_f = float(sp)
                    hands = int(BUDGET // (p_f * 100))
                    st_qty = f"{hands}手（{hands*100}股）" if hands > 0 else "资金不足1手"
            except Exception:
                st_qty = "--"
        st_cards += f'  <div class="st-reason">✅ 可买理由：{sr}</div>'
        st_cards += f'  <div class="st-qty">📌 建议买入股数：<b style="color:#c62828;">{st_qty}</b></div>'
        st_cards += f'  <div class="st-line">📈 涨值巅峰线：<b style="color:#2e7d32;">{stake}元</b> · 🛡 跌停锋线：<b style="color:#c62828;">{sstop}元</b></div>'
        st_cards += f'  <div class="st-ref">买入区间 <b>{sbuy}</b> · 止损 <b>{sstop}</b> · 止盈 <b>{stake}</b></div>'
        st_cards += f'  <div class="st-warn" data-stwarn="{sc}" data-tp="{0 if sstop in ("--","") else sstop}" data-sl="{0 if stake in ("--","") else stake}"></div>'
        st_cards += f'  <div class="st-ops"><button class="st-buy" data-stbuy="1" data-code="{sc}" data-name="{sn}" data-price="{sp_t.replace("¥", "") if sp_t not in ("--","") else 0}">📥 买入</button></div>'
        st_cards += f'</div>'
    st_ts = esc(str(rec.get("ts", "")))
    if not st_ts:
        st_ts = now  # short_term_rec.json 缺 ts 时用本次生成时间兜底，避免显示"目录更新 --"
    ts_part = f" 目录更新 {st_ts}" if st_ts else ""
    short_term_html = (f'<div id="stRecContainer">'
        f'<div class="card short-term">'
        f'  <div class="card-top"><div class="name">📈 今日短线推荐</div>'
        f'    <div class="st-date">{st_date}{ts_part} · 每15分钟刷新</div></div>'
        f'  <div class="st-summary"><b>市场综述：</b>{st_summary}</div>'
        f'  {st_cards}'
        f'  <div class="st-note">⚠️ {st_note}</div>'
        f'</div></div>')
except Exception:
    short_term_html = ""


EXTRA_CSS = '\n  .ui-mask{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5);z-index:100000;display:flex;align-items:center;justify-content:center;padding:20px;}\n  .level-badge{font-size:10px;padding:1px 6px;border-radius:4px;color:#fff;margin-left:4px;font-weight:bold;vertical-align:middle;}\n  .lv-1{background:#c62828;}.lv-2{background:#f57f17;}.lv-3{background:#2e7d32;}\n  .ui-box{background:#fff;border-radius:12px;padding:18px 16px;width:92%;max-width:340px;box-shadow:0 8px 30px rgba(0,0,0,.25);}\n  .ui-title{font-size:15px;font-weight:700;color:#1a237e;margin-bottom:10px;text-align:center;}\n  .ui-input{width:100%;box-sizing:border-box;padding:10px;font-size:14px;border:1px solid #d0d0d0;border-radius:8px;margin:6px 0 4px;}\n  .ui-input:focus{outline:none;border-color:#1a237e;}\n  .ui-msg{font-size:13px;color:#333;line-height:1.6;margin:6px 0;text-align:center;white-space:pre-wrap;}\n  .ui-btns{display:flex;gap:10px;margin-top:14px;}\n  .ui-btn{flex:1;padding:10px 0;border:none;border-radius:8px;font-size:14px;cursor:pointer;}\n  .ui-btn-ok{background:#1a237e;color:#fff;}\n  .ui-btn-cancel{background:#eee;color:#555;}\n  .search-box { display:flex; gap:8px; margin-bottom:10px; position:relative; }\n  .search-box input { flex:1; padding:11px 12px; border:1px solid #d0d0d0; border-radius:8px; font-size:14px; outline:none; }\n  .search-box input:focus { border-color:#1a237e; }\n  .search-box button { padding:11px 16px; background:#1a237e; color:#fff; border:none; border-radius:8px; cursor:pointer; font-size:14px; }\n  #searchResult { display:none; position:absolute; left:0; right:0; top:100%; margin-top:4px; border:1px solid #ddd; border-radius:8px;\n                  background:#fff; box-shadow:0 4px 12px rgba(0,0,0,.15); z-index:1000; max-height:300px; overflow-y:auto; }\n  .budget-panel { font-size:13px; color:#1a237e; background:#e8eaf6; border-radius:8px; padding:10px 12px; margin-bottom:12px; }\n  .budget-panel .fp-row { display:flex; justify-content:space-between; align-items:baseline; padding:3px 0; gap:8px; }\n  .budget-panel .fp-lab { color:#555; font-size:12px; flex-shrink:0; }\n  .budget-panel .fp-val { font-weight:700; text-align:right; }\n  .budget-panel .fp-total { border-top:1px solid #c5cae9; margin-top:4px; padding-top:7px; font-size:14px; }\n  .budget-panel b { cursor:pointer; border-bottom:1px dashed #9fa8da; }\n  .fp-gold { color:#c9a227; font-weight:800; text-shadow:0 0 1px rgba(255,215,0,.6); }\n  .fp-blackgold { color:#8b6914; font-weight:800; }\n  .fp-darkgreen { color:#006400; font-weight:800; }\n    .btn-chart { display:block; width:100%; margin:6px 0; padding:9px; background:#1a237e; color:#fff; border:none; border-radius:8px; font-size:13px; cursor:pointer; }\n  .chart-container { margin-top:8px; border:1px solid #eee; border-radius:8px; padding:8px; background:#fafafa; }\n  .chart-title { font-size:12px; color:#666; margin-bottom:4px; }\n  .chart-container canvas { width:100%; height:200px; }\n  .trend-row { font-size:13px; }\n  .short-term { border:1px solid #ffd54f; }\n  .short-term .name { color:#e65100; }\n  .st-date { font-size:11px; color:#999; }\n  .st-summary { font-size:13px; color:#333; background:#fff8e1; border-radius:6px; padding:7px 10px; margin:6px 0; }\n  .st-item { border-top:1px dashed #eee; padding:8px 0; font-size:13px; }\n  .st-top { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }\n  .st-code { font-size:11px; color:#999; }\n  .st-badge { font-size:11px; color:#fff; background:#2e7d32; border-radius:4px; padding:1px 6px; margin-left:4px; }\n  .st-reason { color:#333; line-height:1.5; margin:3px 0; }\n  .st-ref { color:#555; font-size:12px; }\n  .st-hot { border:1.5px solid #ffd54f; background:#fffde7; }\n  .st-hot-badge { font-size:11px; color:#fff; background:#e65100; border-radius:4px; padding:1px 6px; margin-left:4px; }\n  .st-space { font-size:12px; font-weight:bold; margin-left:4px; }\n  .st-warn { font-size:12px; margin-top:3px; }\n  .st-ops { margin-top:5px; display:flex; gap:8px; }\n  .st-buy { padding:6px 16px; background:#c62828; color:#fff; border:none; border-radius:12px; font-size:12px; cursor:pointer; }\n  .st-sell { padding:6px 16px; background:#2e7d32; color:#fff; border:none; border-radius:12px; font-size:12px; cursor:pointer; }\n  .st-line { font-size:13px; color:#333; margin:2px 0; }\n  .st-qty { font-size:13px; color:#333; margin:2px 0; }\n  .st-note { font-size:11px; color:#999; margin-top:8px; }\n  .advice { margin-top:14px; padding:10px; background:#fff8e1; border:1px solid #ffe082; border-radius:8px; font-size:13px; }\n  .advice-top { font-weight:bold; margin-bottom:4px; }\n  .advice-reason { color:#333; line-height:1.5; margin-bottom:4px; }\n  .advice-risk { color:#c62828; font-size:12px; }\n  .holdings { border:1px solid #dbe4f7; }\n  .h-alert { display:none; background:#ffebee; border:1px solid #ef9a9a; color:#c62828; border-radius:8px; padding:8px 10px; margin:8px 0; font-size:13px; }\n  .h-toolbar { margin:6px 0; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }\n  .h-notify { padding:7px 12px; background:#1a237e; color:#fff; border:none; border-radius:8px; font-size:12px; cursor:pointer; }\n  .h-bak { padding:7px 12px; background:#00695c; color:#fff; border:none; border-radius:8px; font-size:12px; cursor:pointer; }\n  .h-tip { font-size:11px; color:#888; }\n  .h-item { border-top:1px dashed #eee; padding:8px 0; font-size:13px; }\n  .h-top { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }\n  .h-qty { font-size:11px; color:#1a237e; background:#e8eaf6; border-radius:4px; padding:1px 6px; }\n  .h-row { font-size:13px; color:#333; margin:3px 0; }\n  .h-row b { cursor:pointer; border-bottom:1px dashed #bbb; }\n  .h-pnl { font-size:12px; color:#555; margin-top:3px; }\n  .h-empty { color:#999; font-size:13px; padding:8px 0; text-align:center; }\n  .h-hint { font-size:11px; color:#999; margin-top:8px; }\n  .h-ops { padding:3px 10px; background:#fff; border:1px solid #1a237e; color:#1a237e; border-radius:12px; font-size:11px; cursor:pointer; margin-left:auto; }\n  .w-card { border:1px solid #dbe4f7; }\n  .w-item { border-top:1px dashed #eee; padding:8px 0; font-size:13px; cursor:pointer; display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }\n  .w-chg { font-weight:bold; font-size:12px; }\n  .w-tip { font-size:11px; color:#666; width:100%; }\n\n/* ===== 升级版 v2：Tab 分区导航 + 深蓝金主题 ===== */\n  .tabs { display:flex; gap:6px; margin:0 0 14px; position:sticky; top:0; background:#f0f2f5; padding:8px 0; z-index:50; }\n  .tab-btn { flex:1; padding:10px 4px; border:none; border-radius:10px; font-size:13px; font-weight:700; cursor:pointer;\n             background:#fff; color:#1a237e; box-shadow:0 1px 3px rgba(0,0,0,.08); transition:all .15s; }\n  .tab-btn.active { background:#1a237e; color:#ffd54f; box-shadow:0 2px 8px rgba(26,35,126,.35); }\n  .tab-page { display:none; }\n  .tab-page.active { display:block; }\n  .ver-badge { display:inline-block; background:#1a237e; color:#ffd54f; border-radius:6px; padding:1px 8px; font-size:11px; margin-left:6px; }\n  .err-banner { display:none; background:#ffebee; color:#c62828; border:1px solid #ef9a9a; border-radius:8px; padding:8px 10px; font-size:12px; margin-bottom:10px; }\n  .btn-primary { padding:8px 18px; background:#c62828; color:#fff; border:none; border-radius:10px; font-size:13px; cursor:pointer; }\n  .btn-green { padding:8px 18px; background:#2e7d32; color:#fff; border:none; border-radius:10px; font-size:13px; cursor:pointer; }\n  .btn-navy { padding:8px 18px; background:#1a237e; color:#ffd54f; border:none; border-radius:10px; font-size:13px; cursor:pointer; }\n  .quick-ops { display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; }\n  .quick-ops button { flex:1; min-width:100px; }\n  .h-toolbar { margin:6px 0; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }\n  .h-toolbar button { padding:7px 12px; border:none; border-radius:8px; font-size:12px; cursor:pointer; }\n  .h-notify { background:#1a237e; color:#fff; }\n  .h-bak { background:#00695c; color:#fff; }\n  .modal-btn { flex:1; padding:12px; border:none; border-radius:8px; font-size:14px; cursor:pointer; }\n  .modal-btn.buy { background:#c62828; color:#fff; }\n  .modal-btn.sell { background:#2e7d32; color:#fff; }\n  .modal-btn.gray { background:#eee; color:#555; }\n  @media (max-width:420px) { .tab-btn { font-size:12px; padding:9px 2px; } }'
SEARCH_HTML = '<div class="search-box">\n  <input type="text" id="searchInput" placeholder="输入股票代码/名称，全网搜索..." onkeydown="if(event.key===\'Enter\')searchStocks()">\n  <button onclick="searchStocks()">🔍 搜索</button>\n  <div id="searchResult"></div>\n</div>\n<div class="budget-panel" id="fundPanel"></div>\n'
APP_JS_EXTRA = "\n// ===== 实时价格刷新（O(n)优化版：缓存DOM映射，避免每只股票全页扫描） =====\nvar _lastPrice = {}, _lastChg = {}, _lastVol = {}, _lastTr = {}, _lastAmp = {}, _lastAvg = {}, _lastHigh = {}, _lastLow = {}, _lastO = {}, _lastI = {}, _lastPrev = {};\nvar _priceMap = null;\nfunction getPriceMap(){\n  if(_priceMap) return _priceMap;\n  _priceMap = { p:{}, c:{} };\n  document.querySelectorAll('.price[data-code]').forEach(function(el){ _priceMap.p[el.getAttribute('data-code')] = el; });\n  document.querySelectorAll('.chg[data-code]').forEach(function(el){ _priceMap.c[el.getAttribute('data-code')] = el; });\n  return _priceMap;\n}\nfunction refreshRealtime(){\n  var codes = [];\n  document.querySelectorAll('.price[data-code]').forEach(function(el){ var c=el.getAttribute('data-code'); if(codes.indexOf(c)<0) codes.push(c); });\n  var wl=collectWatchCodes(); wl.forEach(function(x){ if(codes.indexOf(x.code)<0) codes.push(x.code); });\n  if(!codes.length) return;\n  var m = getPriceMap();\n  var s = document.createElement('script');\n  s.src = 'https://qt.gtimg.cn/q=' + codes.join(',');\n  s.onload = function(){\n    try{\n      codes.forEach(function(code){\n        var data = window['v_' + code];\n        if(!data || typeof data !== 'string') return;\n        var parts = data.split('~');\n        if(parts.length < 35) return;\n        var price = parseFloat(parts[3]), chg = parseFloat(parts[32]);\n        if(!price || isNaN(price)) return;\n        var pe = m.p[code], ce = m.c[code];\n        _lastPrice[code] = price; _lastChg[code] = chg; _lastPrev[code] = parseFloat(parts[4]); _lastVol[code] = parseFloat(parts[49]); _lastTr[code] = parseFloat(parts[38]); _lastAmp[code] = parseFloat(parts[43]); _lastAvg[code] = parseFloat(parts[51]); _lastHigh[code] = parseFloat(parts[33]); _lastLow[code] = parseFloat(parts[34]); _lastO[code] = parseFloat(parts[7]); _lastI[code] = parseFloat(parts[8]);\n        if(pe) pe.textContent = '¥' + price.toFixed(2);\n        if(ce){ ce.textContent = (chg>=0?'+':'') + chg.toFixed(2) + '%'; ce.style.color = chg>=0 ? '#d32f2f' : '#2e7d32'; }\n      });\n    }catch(e){ console.log('实时解析失败', e); }\n    if(s.parentNode) s.parentNode.removeChild(s);\n  };\n  s.onerror = function(){ if(s.parentNode) s.parentNode.removeChild(s); };\n  document.body.appendChild(s);\n}\nsetInterval(refreshRealtime, 1000); // 每支股票每秒更新现价\nsetTimeout(refreshRealtime, 300);    // 页面加载后先刷一次\n\n// ===== K线图（canvas高清适配 devicePixelRatio） =====\nfunction toggleChart(chartId){\n  var chart = document.getElementById(chartId);\n  if(!chart) return;\n  if(chart.style.display === 'none'){ chart.style.display = 'block'; drawKLine(chartId.replace('chart_','')); }\n  else chart.style.display = 'none';\n}\nfunction drawKLine(code){\n  var canvas = document.getElementById('canvas_' + code);\n  var dataEl = document.getElementById('data_' + code);\n  if(!canvas || !dataEl) return;\n  var data;\n  try{ data = JSON.parse(dataEl.textContent); }catch(e){ return; }\n  if(!data || !data.length) return;\n  var dpr = window.devicePixelRatio || 1;\n  var w = canvas.clientWidth || canvas.width;\n  var h = 200;\n  canvas.width = w * dpr; canvas.height = h * dpr;\n  canvas.style.width = w + 'px'; canvas.style.height = h + 'px';\n  var ctx = canvas.getContext('2d');\n  ctx.scale(dpr, dpr);\n  ctx.clearRect(0, 0, w, h);\n  var maxPrice = 0, minPrice = Infinity;\n  data.forEach(function(d){ if(d[3] > maxPrice) maxPrice = d[3]; if(d[4] < minPrice) minPrice = d[4]; });\n  var range = maxPrice - minPrice || 1;\n  var pad = 12, cw = w - pad*2, ch = h - pad*2;\n  var bw = cw / data.length * 0.6;\n  data.forEach(function(d, i){\n    var x = pad + (cw/data.length)*i + (cw/data.length - bw)/2;\n    var yo = pad + ch*(1-(d[1]-minPrice)/range);\n    var yc = pad + ch*(1-(d[2]-minPrice)/range);\n    var yh = pad + ch*(1-(d[3]-minPrice)/range);\n    var yl = pad + ch*(1-(d[4]-minPrice)/range);\n    var col = d[2] >= d[1] ? '#d32f2f' : '#2e7d32';\n    ctx.strokeStyle = col; ctx.fillStyle = col;\n    ctx.beginPath(); ctx.moveTo(x+bw/2, yh); ctx.lineTo(x+bw/2, yl); ctx.stroke();\n    var yTop = Math.min(yo, yc), bh = Math.max(Math.abs(yc-yo), 1);\n    ctx.fillRect(x, yTop, bw, bh);\n  });\n  // 收盘价折线（叠加在K线上，蓝色）\n  ctx.strokeStyle = 'rgba(26,35,126,0.8)'; ctx.lineWidth = 1.5; ctx.beginPath();\n  data.forEach(function(d, i){\n    var x = pad + (cw/data.length)*i + (cw/data.length - bw)/2 + bw/2;\n    var yc = pad + ch*(1-(d[2]-minPrice)/range);\n    if(i===0) ctx.moveTo(x, yc); else ctx.lineTo(x, yc);\n  });\n  ctx.stroke(); ctx.lineWidth = 1;\n  ctx.fillStyle = '#666'; ctx.font = '10px sans-serif';\n  ctx.fillText(maxPrice.toFixed(2), 3, pad+8);\n  ctx.fillText(minPrice.toFixed(2), 3, h-2);\n  ctx.fillStyle = 'rgba(26,35,126,0.9)';\n  ctx.fillText('—— 收盘价', w-70, pad+8);\n}\n"

APP_JS = '\n// ===== ES5 兼容兜底（微信X5/老内核无 padStart） =====\nif(!String.prototype.padStart){ String.prototype.padStart=function(len,ch){ ch=ch||\'0\'; var s=String(this); while(s.length<len){ s=ch+s; } return s; }; }\n// ===== 移动端友好弹窗（替代 prompt/alert，避免 iOS/微信 prompt 失效） =====\nfunction _uiRemove(){ var m=document.getElementById(\'uiMask\'); if(m&&m.parentNode) m.parentNode.removeChild(m); }\nvar _uiCb=null;\nfunction uiPromptOk(){ var inp=document.getElementById(\'uiInp\'); var v=inp?inp.value:\'\'; _uiRemove(); if(_uiCb){ var c=_uiCb; _uiCb=null; c(v); } }\nfunction uiAlert(msg){ var old=document.getElementById(\'uiMask\'); if(old&&old.parentNode) old.parentNode.removeChild(old);var mask=document.createElement(\'div\'); mask.id=\'uiMask\'; mask.className=\'ui-mask\';mask.innerHTML=\'<div class="ui-box"><div class="ui-title">提示</div><div class="ui-msg">\'+String(msg).replace(/</g,\'&lt;\').replace(/\\n/g,\'<br>\')+\'</div><div class="ui-btns"><button class="ui-btn ui-btn-ok" onclick="_uiRemove()">确定</button></div></div>\';document.body.appendChild(mask); }\nfunction uiPrompt(title,def,cb){ var old=document.getElementById(\'uiMask\'); if(old&&old.parentNode) old.parentNode.removeChild(old);var mask=document.createElement(\'div\'); mask.id=\'uiMask\'; mask.className=\'ui-mask\'; _uiCb=cb;mask.innerHTML=\'<div class="ui-box"><div class="ui-title">\'+String(title||\'\').replace(/</g,\'&lt;\')+\'</div><input class="ui-input" id="uiInp" type="text" value=""><div class="ui-btns"><button class="ui-btn ui-btn-cancel" onclick="_uiRemove()">取消</button><button class="ui-btn ui-btn-ok" onclick="uiPromptOk()">确定</button></div></div>\';document.body.appendChild(mask);var inp=document.getElementById(\'uiInp\'); inp.value=(def===undefined?\'\':def); inp.focus();inp.onkeydown=function(e){ if(e.key===\'Enter\'){ uiPromptOk(); } }; }\nfunction getBudget(){ try{ return parseFloat(localStorage.getItem(\'myBudget\'))||1000; }catch(e){ return 1000; } }\nfunction setBudget(v){ localStorage.setItem(\'myBudget\', v); refreshFundPanel(); }\nfunction getPrincipal(){ try{ var v=parseFloat(localStorage.getItem(\'myPrincipal\')); return (!v||v<=0)?1000:v; }catch(e){ return 1000; } }\nfunction setPrincipal(v){ localStorage.setItem(\'myPrincipal\', v); refreshFundPanel(); }\nfunction editPrincipal(){ uiPrompt(\'请输入本金（元）：\', getPrincipal(), function(v){ if(!v||isNaN(v)||parseFloat(v)<=0) return; setPrincipal(parseFloat(v)); }); }\nfunction editBudget(){ uiPrompt(\'请输入可用资金（元）：\', getBudget(), function(v){ if(!v||isNaN(v)||parseFloat(v)<=0) return; setBudget(parseFloat(v)); }); }\nfunction todayKey(){ var d=new Date(); return d.getFullYear()+\'/\'+(d.getMonth()+1)+\'/\'+d.getDate(); }\nfunction calcTodayPnL(){ var tk=todayKey(), pnl=0;\n  getTrades().forEach(function(t){ if((t.time||\'\').indexOf(tk)!==0) return; var pc=_lastPrev[t.code]; if(t.type===\'sell\'&&pc) pnl+=(t.price-pc)*t.qty; });\n  getHoldings().forEach(function(h){ var p=_lastPrice[h.code], pc=_lastPrev[h.code]; if(p&&pc) pnl+=(p-pc)*h.qty; });\n  return pnl; }\nfunction calcFund(){ var mv=0; getHoldings().forEach(function(h){ mv+=(_lastPrice[h.code]||0)*h.qty; }); var cash=getBudget(), principal=getPrincipal(), total=cash+mv; return { cash:cash, principal:principal, mv:mv, total:total, totalPnl:total-principal, todayPnl:calcTodayPnL() }; }\nfunction pnlClass(v,kind){ if(v>0) return kind===\'today\'?\'fp-gold\':\'fp-blackgold\'; if(v<0) return \'fp-darkgreen\'; return \'\'; }\nfunction pnlTxt(v){ return (v>0?\'+\':\'\')+v.toFixed(2)+\'元\'; }\nfunction refreshFundPanel(){ var el=document.getElementById(\'fundPanel\'); if(!el) return; var f=calcFund(); var h=\'\';\n  h+=\'<div class="fp-row fp-total"><span class="fp-lab">总资产（现金+持仓）</span><span class="fp-val">¥\'+f.total.toFixed(2)+\'</span></div>\';\n  h+=\'<div class="fp-row"><span class="fp-lab">本金</span><span class="fp-val" onclick="editPrincipal()" title="点击编辑本金">¥\'+f.principal.toFixed(2)+\'</span></div>\';\n  h+=\'<div class="fp-row"><span class="fp-lab">可用资金</span><span class="fp-val" onclick="editBudget()" title="点击编辑可用资金">¥\'+f.cash.toFixed(2)+\'</span></div>\';\n  h+=\'<div class="fp-row"><span class="fp-lab">持仓市值</span><span class="fp-val">¥\'+f.mv.toFixed(2)+\'</span></div>\';\n  h+=\'<div class="fp-row"><span class="fp-lab">今日盈亏</span><span class="fp-val \'+pnlClass(f.todayPnl,\'today\')+\'">\'+pnlTxt(f.todayPnl)+\'</span></div>\';\n  h+=\'<div class="fp-row"><span class="fp-lab">长期盈亏（累计）</span><span class="fp-val \'+pnlClass(f.totalPnl,\'total\')+\'">\'+pnlTxt(f.totalPnl)+\'</span></div>\';\n  el.innerHTML=h; }\n\n// ===== 交易费用 =====\nfunction calcFee(amount,type){ var c=Math.max(amount*0.00025,5); var t=amount*0.00002; var s=0; if(type===\'sell\') s=amount*0.001; return c+t+s; }\n\n// ===== 交易记录（存本地，双键冗余防丢失） =====\nfunction getTrades(){ try{ var v=JSON.parse(localStorage.getItem(\'myTrades\')||\'\'); if(Array.isArray(v)) return v; }catch(e){} try{ var b=JSON.parse(localStorage.getItem(\'myTrades_bak\')||\'\'); if(Array.isArray(b)) return b; }catch(e){} return []; }\nfunction saveTrades(t){ var s=JSON.stringify(t); localStorage.setItem(\'myTrades\', s); try{ localStorage.setItem(\'myTrades_bak\', s); }catch(e){} }\n// ===== 数据备份/恢复（防浏览器清空丢失持仓） =====\nfunction exportBackup(){\n  var p={trades:getTrades(), budget:getBudget(), principal:getPrincipal(), hs:loadHS(), alerts:loadAlerts(), adv:localStorage.getItem(\'myAdviceHour\')||\'\', ver:1, ts:new Date().toLocaleString(\'zh-CN\')};\n  var s=JSON.stringify(p);\n  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(s).then(function(){ uiAlert(\'✅ 备份已复制到剪贴板！\\n请粘贴保存到记事本/备忘录（含全部交易记录、资金、本金、预警设置）。\\n数据丢失时用「恢复数据」粘贴回来。\'); }).catch(function(){ uiPrompt(\'✅ 备份数据（请复制保存）：\', s, function(){}); }); }\n  else{ uiPrompt(\'✅ 备份数据（请复制保存）：\', s, function(){}); }\n}\nfunction importBackup(){\n  uiPrompt(\'请粘贴备份数据（JSON）：\', \'\', function(s){ if(!s) return;\n  var p; try{ p=JSON.parse(s); }catch(e){ uiAlert(\'❌ 备份数据格式无效，无法恢复\'); return; }\n  if(!p || !Array.isArray(p.trades)){ uiAlert(\'❌ 备份缺少交易记录，恢复失败\'); return; }\n  saveTrades(p.trades);\n  if(typeof p.budget===\'number\' && p.budget>0) setBudget(p.budget);\n  if(typeof p.principal===\'number\' && p.principal>0) setPrincipal(p.principal);\n  if(p.hs && typeof p.hs===\'object\') saveHS(p.hs);\n  if(p.alerts && typeof p.alerts===\'object\') saveAlerts(p.alerts);\n  uiAlert(\'✅ 数据恢复成功！\\n已恢复交易记录 \'+(p.trades||[]).length+\' 条。\\n持仓与资金面板已刷新。\');\n  refreshHoldings(); refreshFundPanel(); refreshWatch();\n  });\n}\n\n// ===== 全网搜索（JSONP，腾讯smartbox） =====\nfunction searchStocks(){\n  var keyword=(document.getElementById(\'searchInput\').value||\'\').trim();\n  if(!keyword) return;\n  var div=document.getElementById(\'searchResult\');\n  var cb=\'searchCb_\'+Date.now();\n  window[cb]=function(results){\n    try{\n      if(!results||!results.length){ div.innerHTML=\'<div style="padding:14px;color:#999;font-size:13px;">未找到匹配股票</div>\'; div.style.display=\'block\'; return; }\n      var html=\'\';\n      results.forEach(function(r){\n        html+=\'<div onclick="closeSearch();showStockDetail(\\\'\'+r.code+\'\\\',\\\'\'+r.name+\'\\\')" style="padding:10px 14px;border-bottom:1px solid #f0f0f0;cursor:pointer;display:flex;justify-content:space-between;align-items:center;">\';\n        html+=\'<span><b style="color:#1a237e;">\'+r.name+\'</b></span><span style="color:#666;font-size:12px;background:#f5f5f5;padding:2px 8px;border-radius:4px;">\'+r.code+\'</span></div>\';\n      });\n      div.innerHTML=html; div.style.display=\'block\';\n    }finally{ delete window[cb]; var s=document.getElementById(cb); if(s&&s.parentNode) s.parentNode.removeChild(s); }\n  };\n  var s=document.createElement(\'script\'); s.id=cb;\n  s.src=\'https://smartbox.gtimg.cn/s3/?v=2&q=\'+encodeURIComponent(keyword)+\'&t=all\';\n  s.onload=function(){\n    try{\n      var raw=window.v_hint||\'\';\n      if(!raw){ div.innerHTML=\'<div style="padding:14px;color:#999;font-size:13px;">未找到匹配股票</div>\'; div.style.display=\'block\'; }\n      else{\n        var results=[];\n        raw.split(\'^\').forEach(function(item){ var p=item.split(\'~\'); if(p.length>=3&&p[4]&&p[4].indexOf(\'GP\')===0){ results.push({code:p[0]+p[1],name:p[2]}); } });\n        window[cb](results);\n      }\n    }catch(e){ console.log(\'搜索解析失败\',e); }\n    if(s.parentNode) s.parentNode.removeChild(s);\n  };\n  s.onerror=function(){ div.innerHTML=\'<div style="padding:14px;color:#999;font-size:13px;">搜索失败，请检查网络后重试</div>\'; div.style.display=\'block\'; if(s.parentNode) s.parentNode.removeChild(s); };\n  document.body.appendChild(s);\n}\nfunction closeSearch(){ var div=document.getElementById(\'searchResult\'); if(div) div.style.display=\'none\'; }\n\n// ===== 行情详情（JSONP，腾讯qt） =====\nfunction showStockDetail(code,name){\n  closeSearch();\n  var cb=\'detailCb_\'+Date.now();\n  window[cb]=function(){\n    try{\n      var data=window[\'v_\'+code];\n      if(!data||typeof data!==\'string\'){ uiAlert(\'获取行情失败，请重试\'); return; }\n      var parts=data.split(\'~\');\n      if(parts.length<35) return;\n      var price=parseFloat(parts[3]),chg=parseFloat(parts[32]),open=parseFloat(parts[5]),high=parseFloat(parts[33]),low=parseFloat(parts[34]),prevClose=parseFloat(parts[4]);\n      var turnover=parseFloat(parts[38]);\n      var lu=parseFloat(parts[47]),ld=parseFloat(parts[48]);\n      if(lu&&ld&&lu>0){ limitUp=lu.toFixed(2); limitDown=ld.toFixed(2); }\n      var volRatio=parseFloat(parts[49]),avg=parseFloat(parts[51]),outer=parseFloat(parts[7]),inner=parseFloat(parts[8]);\n      var adviceHtml=buildAdvice(price,chg,outer,inner,avg,volRatio,turnover,parseFloat(limitUp),parseFloat(limitDown)).html;\n      var limitUp=(prevClose*1.1).toFixed(2),limitDown=(prevClose*0.9).toFixed(2);\n      var chgColor=chg>=0?\'#c62828\':\'#2e7d32\';\n      var budget=getBudget();\n      var maxQty=Math.floor(budget/price/100)*100;\n      var canBuy=maxQty>=100;\n      var html=\n        \'<div style="position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;">\'+\n        \'<div style="background:#fff;border-radius:12px;padding:20px;width:90%;max-width:400px;max-height:80vh;overflow-y:auto;">\'+\n        \'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">\'+\n        \'<div><h2 style="margin:0;font-size:18px;">\'+name+\'</h2><div style="color:#666;font-size:12px;">\'+code+\'</div></div>\'+\n        \'<button onclick="closeStockDetail()" style="background:#eee;border:none;border-radius:50%;width:32px;height:32px;cursor:pointer;font-size:16px;">×</button></div>\'+\n        \'<div style="text-align:center;margin:20px 0;">\'+\n        \'<div style="font-size:36px;font-weight:bold;color:\'+chgColor+\';">¥\'+price.toFixed(2)+\'</div>\'+\n        \'<div style="color:\'+chgColor+\';font-size:16px;">\'+(chg>=0?\'+\':\'\')+chg.toFixed(2)+\'%</div></div>\'+\n        \'<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:13px;">\'+\n        \'<div>今开：¥\'+open.toFixed(2)+\'</div><div>昨收：¥\'+prevClose.toFixed(2)+\'</div>\'+\n        \'<div>最高：¥\'+high.toFixed(2)+\'</div><div>最低：¥\'+low.toFixed(2)+\'</div>\'+\n        \'<div style="color:#c62828;">涨停：¥\'+limitUp+\'</div><div style="color:#2e7d32;">跌停：¥\'+limitDown+\'</div></div>\'+\n        adviceHtml+\n        \'<div style="margin-top:16px;padding:10px;background:#e8eaf6;border-radius:8px;font-size:13px;text-align:center;">\'+\n        \'可用资金：<b>¥\'+budget.toFixed(2)+\'</b> · 当前价可买：<b>\'+(canBuy?maxQty+\'股\':\'资金不足\')+\'</b></div>\'+\n        \'<div style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap;">\'+\n        \'<button onclick="buyWithBudget(\\\'\'+code+\'\\\',\\\'\'+name+\'\\\',\'+price+\')" style="flex:1;padding:12px;background:#c62828;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;\'+(canBuy?\'\':\'opacity:0.5;\')+\'">⚡一键模拟买入</button>\'+\n        \'<button onclick="recordTrade(\\\'buy\\\',\\\'\'+code+\'\\\',\\\'\'+name+\'\\\',\'+price+\')" style="flex:1;padding:12px;background:#ff7043;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;">记录买入</button></div>\'+\n        \'<div style="margin-top:8px;"><button onclick="recordTrade(\\\'sell\\\',\\\'\'+code+\'\\\',\\\'\'+name+\'\\\',\'+price+\')" style="width:100%;padding:12px;background:#2e7d32;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;">记录卖出</button></div>\'+\n        \'<div style="margin-top:12px;font-size:11px;color:#999;text-align:center;">⚠️ 模拟交易仅记录本地，不连接券商，不构成投资建议</div>\'+\n        \'</div></div>\';\n      var m=document.getElementById(\'stockDetailModal\');\n      if(!m){ m=document.createElement(\'div\'); m.id=\'stockDetailModal\'; document.body.appendChild(m); }\n      m.innerHTML=html; m.style.display=\'block\';\n    }catch(e){ console.log(\'详情解析失败\',e); uiAlert(\'获取行情失败，请重试\'); }\n    finally{ delete window[cb]; var s=document.getElementById(cb); if(s&&s.parentNode) s.parentNode.removeChild(s); }\n  };\n  var s=document.createElement(\'script\'); s.id=cb; s.src=\'https://qt.gtimg.cn/q=\'+code;\n  s.onload=function(){ try{ window[cb](); }catch(e){} if(s.parentNode) s.parentNode.removeChild(s); };\n  s.onerror=function(){ try{ window[cb](); }catch(e){ uiAlert(\'获取行情失败，请检查网络\'); } if(s.parentNode) s.parentNode.removeChild(s); };\n  document.body.appendChild(s);\n}\nfunction closeStockDetail(){ var m=document.getElementById(\'stockDetailModal\'); if(m) m.style.display=\'none\'; }\n\n// ===== 一键模拟买入 =====\nfunction buyWithBudget(code,name,price){\n  var budget=getBudget();\n  var maxQty=Math.floor(budget/price/100)*100;\n  if(maxQty<100){ uiAlert(\'可用资金不足！\\n可用资金：¥\'+budget.toFixed(2)+\'\\n当前价：¥\'+price.toFixed(2)+\'\\n买1手(100股)需要：¥\'+(price*100).toFixed(2)); return; }\n  var amount=price*maxQty, fee=calcFee(amount,\'buy\'), total=amount+fee;\n  if(total>budget){ var q2=maxQty-100; if(q2<100){ uiAlert(\'交易费用后资金不足，请调整\'); return; } maxQty=q2; amount=price*maxQty; fee=calcFee(amount,\'buy\'); total=amount+fee; }\n  var trades=getTrades();\n  trades.push({type:\'buy\',code:code,name:name,price:price,qty:maxQty,amount:amount,fee:fee,time:new Date().toLocaleString(\'zh-CN\'),auto:true});\n  saveTrades(trades);\n  setBudget(budget-total);\n  uiAlert(\'⚡ 模拟买入成功！\\n\'+name+\' \'+maxQty+\'股 @ ¥\'+price.toFixed(2)+\'\\n成交金额：¥\'+amount.toFixed(2)+\'\\n交易费用：¥\'+fee.toFixed(2)+\'\\n实际支出：¥\'+total.toFixed(2)+\'\\n剩余资金：¥\'+(budget-total).toFixed(2));\n  closeStockDetail();\n}\n\n// ===== 手动记录交易 =====\nfunction recordTrade(type,code,name,price){\n  uiPrompt(\'请输入实际成交价格（元）：\',price, function(pi){ if(!pi||isNaN(pi)||parseFloat(pi)<=0) return;\n  uiPrompt(\'请输入数量（股），1手=100股：\',\'100\', function(qi){ if(!qi||isNaN(qi)||parseInt(qi)<=0) return;\n  var p=parseFloat(pi), q=parseInt(qi), amt=p*q, fee=calcFee(amt,type);\n  if(type===\'sell\'){ var curH=getHoldings().filter(function(h){return h.code===code;})[0]; if(!curH||curH.qty<q){ uiAlert(\'持仓不足！当前持有 \'+(curH?curH.qty:0)+\' 股，无法卖出 \'+q+\' 股\'); return; } }\n  var trades=getTrades(); trades.push({type:type,code:code,name:name,price:p,qty:q,amount:amt,fee:fee,time:new Date().toLocaleString(\'zh-CN\')}); saveTrades(trades);\n  uiAlert((type===\'buy\'?\'买入\':\'卖出\')+\'记录成功：\\n\'+name+\' \'+q+\'股 @ ¥\'+p.toFixed(2)+\'\\n成交金额：¥\'+amt.toFixed(2)+\'\\n交易费用：¥\'+fee.toFixed(2)+\'\\n实际支出：¥\'+(amt+fee).toFixed(2));\n  closeStockDetail();\n  }); });\n}\n\nrefreshFundPanel();\n'

APP_JS_ADVICE = """\n// ===== 买卖建议（搜索详情：买盘/分时/量能/换手/追高 五维汇总） =====
function buildAdvice(price,chg,outer,inner,avg,volRatio,turnover,limitUp,limitDown){
  var score=0,reasons=[],risks=[];
  function w(v){ return (v/10000).toFixed(1); }
  if(outer>inner*1.1){ score+=1; reasons.push('外盘'+w(outer)+'万手>内盘'+w(inner)+'万手，买盘占优'); }
  else if(inner>outer*1.1){ score-=1; reasons.push('内盘'+w(inner)+'万手>外盘'+w(outer)+'万手，卖压偏大'); }
  if(avg&&price>avg*1.001){ score+=1; reasons.push('现价'+price.toFixed(2)+'站上分时均价'+avg.toFixed(2)); }
  else if(avg&&price<avg*0.999){ score-=1; reasons.push('现价'+price.toFixed(2)+'跌破分时均价'+avg.toFixed(2)); }
  if(volRatio>=1.5){ score+=1; reasons.push('量比'+volRatio.toFixed(1)+'放量上攻'); }
  else if(volRatio>0&&volRatio<0.7){ score-=1; reasons.push('量比'+volRatio.toFixed(1)+'缩量动能不足'); }
  if(turnover>=3){ score+=1; reasons.push('换手'+turnover.toFixed(1)+'%活跃'); }
  else if(turnover>0&&turnover<1){ score-=1; reasons.push('换手'+turnover.toFixed(1)+'%清淡'); }
  if(chg>=9.4){ score-=1.5; risks.push('已接近涨停'+chg.toFixed(1)+'%，追高风险大'); }
  else if(chg<=-4){ score-=1; risks.push('跌幅'+chg.toFixed(1)+'%走弱，不宜抢反弹'); }
  else if(chg>0){ score+=0.5; reasons.push('今日上涨'+chg.toFixed(1)+'%'); }
  var dist=(limitUp-price)/price*100;
  if(dist>=0&&dist<2){ risks.push('距涨停仅'+dist.toFixed(1)+'%，追高易被套'); }
  var verdict,color;
  if(score>=2.5){ verdict='建议买入（短线）'; color='#c62828'; }
  else if(score>=-0.5){ verdict='观望（等回踩）'; color='#f9a825'; }
  else { verdict='暂不建议买入'; color='#2e7d32'; }
  if(!reasons.length) reasons.push('信号中性，无明显方向');
  var html='<div class="advice"><div class="advice-top">📊 买卖建议：<b style="color:'+color+';">'+verdict+'</b></div>'+
    '<div class="advice-reason">理由：'+reasons.join('；')+'</div>'+
    (risks.length?'<div class="advice-risk">⚠️ 风险：'+risks.join('；')+'</div>':'')+'</div>';
  return {verdict:verdict,color:color,reasons:reasons,risks:risks,html:html};
}
"""

HOLD_HTML = """<div class="card holdings">
  <div class="card-top"><div class="name">📊 我的持仓 · 实时跟进</div><div class="st-date">每10秒刷新</div></div>
  <div class="h-alert" id="alertBar" style="display:none"></div>
  <div class="h-toolbar"><button onclick="enableNotify()" class="h-notify">🔔 开启预警通知</button><button onclick="showMonthReport()" class="h-notify" style="background:#e65100;">📊 本月持仓报告</button><button onclick="exportBackup()" class="h-bak">💾 备份数据</button><button onclick="importBackup()" class="h-bak" style="background:#455a64;">♻️ 恢复数据</button><span class="h-tip">点击数值可修改上涨预估/跌停峰值</span></div>
  <div id="holdingsList"><div class="h-empty">暂无持仓：搜索买入后自动实时跟进</div></div>
  <div class="h-hint">⚠️ 模拟交易仅记录本地，不构成投资建议；页面打开时跌近跌停峰值自动预警提醒</div>
</div>
<div class="card w-card">
  <div class="card-top"><div class="name">📡 未持股 · 波动推荐</div><div class="st-date">涨跌幅≥4%异动自动提示</div></div>
  <div id="watchList"><div class="h-empty">暂无波动异动：历史买卖/今日推荐标的出现≥4%波动时自动推荐</div></div>
  <div class="h-hint">点击标的名查看详情与买卖建议 · 模拟交易仅记录本地，不构成投资建议</div>
</div>"""

APP_JS_HOLD = """\n// ===== 我的持仓：实时跟进 + 上涨预估 + 跌停峰值 + 跌近峰值预警 =====
function loadHS(){ try{ return JSON.parse(localStorage.getItem('myHS')||'{}'); }catch(e){ return {}; } }
function saveHS(o){ localStorage.setItem('myHS', JSON.stringify(o)); }
function loadAlerts(){ try{ return JSON.parse(localStorage.getItem('myAlerts')||'{}'); }catch(e){ return {}; } }
function saveAlerts(o){ localStorage.setItem('myAlerts', JSON.stringify(o)); }
function getHoldings(){
  var trades=getTrades(), h={};
  trades.forEach(function(t){
    if(!h[t.code]) h[t.code]={code:t.code,name:t.name,qty:0,cost:0,fee:0};
    if(t.type==='buy'){ h[t.code].qty+=t.qty; h[t.code].cost+=t.amount; h[t.code].fee+=t.fee; }
    else if(t.type==='sell'){ if(h[t.code].qty>0){ var _avg=h[t.code].cost/h[t.code].qty; h[t.code].cost-=_avg*t.qty; } h[t.code].qty-=t.qty; h[t.code].fee+=t.fee; }
  });
  var out=[];
  for(var k in h){ if(h[k]&&h[k].qty>0) out.push(h[k]); }
  return out;
}
function editLine(code,kind){
  var hs=getHoldings(), one=null;
  hs.forEach(function(x){ if(x.code===code) one=x; });
  if(!one) return;
  var cost=one.cost/one.qty;
  var s=loadHS()[code]||{};
  var cur=kind==='tp'?(s.tp||(cost*1.10).toFixed(2)):(s.sl||(cost*0.95).toFixed(2));
  var v0=kind==='tp'?'输入上涨预估（止盈目标价）：':'输入跌停峰值（止损价）：';
  uiPrompt(v0, cur, function(v){
    if(!v||isNaN(v)||parseFloat(v)<=0) return;
    var hs2=loadHS(); hs2[code]=hs2[code]||{}; hs2[code][kind]=parseFloat(v).toFixed(2); saveHS(hs2);
    refreshHoldings();
  });
}
// ===== 本月持仓报告：真实盈亏（本地记录）+ 大师建议汇总 =====
function showMonthReport(){
  var trades=getTrades(), now=new Date();
  var bCount=0,sCount=0,mbuy=0,msell=0;
  trades.forEach(function(t){
    var parts=(t.time||'').split('/');
    if(parts.length>=2&&parts[0]===String(now.getFullYear())&&parseInt(parts[1])===now.getMonth()+1){
      if(t.type==='buy'){ bCount++; mbuy+=t.amount||0; }
      else { sCount++; msell+=t.amount||0; }
    }
  });
  var hs=getHoldings(), mv=0, mpnl=0;
  hs.forEach(function(h){ var p=_lastPrice[h.code]||0; var cost=h.cost/h.qty; mv+=p*h.qty; mpnl+=(p-cost)*h.qty; });
  var sellN=0, addN=0;
  hs.forEach(function(h){ var a=holdAdviceFor(h); if(a){ if(a.advice==='SELL') sellN++; else if(a.advice==='ADD') addN++; } });
  var pnlCol=mpnl>=0?'color:#d4a017;font-weight:800;':'color:#2e7d32;font-weight:800;';
  var html='<div style="position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;">'+
    '<div style="background:#fff;border-radius:12px;padding:20px;width:90%;max-width:400px;max-height:80vh;overflow-y:auto;">'+
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">'+
    '<h2 style="margin:0;font-size:18px;">📊 '+now.getFullYear()+'年'+(now.getMonth()+1)+'月 持仓报告</h2>'+
    '<button onclick="closeMonthReport()" style="background:#eee;border:none;border-radius:50%;width:32px;height:32px;cursor:pointer;font-size:16px;">×</button></div>'+
    '<div style="background:#e8eaf6;border-radius:8px;padding:10px;font-size:13px;margin-bottom:12px;">'+
    '本月买入 <b>'+bCount+'笔</b>（¥'+mbuy.toFixed(2)+'） · 卖出 <b>'+sCount+'笔</b>（¥'+msell.toFixed(2)+'）</div>'+
    '<div style="background:#fff8e1;border-radius:8px;padding:10px;font-size:13px;margin-bottom:12px;">'+
    '当前持仓 <b>'+hs.length+'只</b> · 持仓市值 <b>¥'+mv.toFixed(2)+'</b><br>'+
    '浮动盈亏 <b style="'+pnlCol+'">'+(mpnl>=0?'+':'')+mpnl.toFixed(2)+'元</b></div>'+
    '<div style="background:#ffebee;border-radius:8px;padding:10px;font-size:13px;margin-bottom:12px;">'+
    '大师建议：<b style="color:#c62828;">'+sellN+'只建议抛售</b> · <b style="color:#2e7d32;">'+addN+'只建议加仓</b><br>'+
    '<span style="font-size:12px;color:#666;">建议明细见各持仓卡「操作建议」行，每小时汇总提示一次</span></div>'+
    '<div style="font-size:12px;color:#333;line-height:1.6;background:#f5f5f5;border-radius:8px;padding:10px;">'+
    '📌 月度调整方向：<br>'+
    (sellN>0?'· 对建议抛售的持仓，按大师止损纪律分批止盈/止损，不恋战<br>':'')+
    (addN>0?'· 对建议加仓的持仓，可在回踩关键点分批摊薄，控制单笔仓位<br>':'')+
    '· 盈利品种守住涨值巅峰线，达目标落袋；亏损品种守住跌停锋线，破位即走<br>'+
    '· 每月最后一周注意控制仓位与回撤，短线不满仓</div>'+
    '<div style="margin-top:12px;font-size:11px;color:#999;text-align:center;">⚠️ 模拟交易仅记录本地，不连接券商，不构成投资建议</div>'+
    '</div></div>';
  var m=document.getElementById('monthReportModal');
  if(!m){ m=document.createElement('div'); m.id='monthReportModal'; document.body.appendChild(m); }
  m.innerHTML=html; m.style.display='block';
}
function closeMonthReport(){ var m=document.getElementById('monthReportModal'); if(m) m.style.display='none'; }
function refreshHoldings(){
  var hs=getHoldings(), el=document.getElementById('holdingsList');
  if(!el) return;
  if(!hs.length){ _priceMap=null; el.innerHTML='<div class="h-empty">暂无持仓：搜索买入后自动实时跟进</div>'; return; }
  var s=loadHS();
  var html='';
  hs.forEach(function(h){
    var cost=h.cost/h.qty;
    var lp=_lastPrice[h.code], lc=_lastChg[h.code];
    var lcTxt=lp?((lc>=0?'+':'')+lc.toFixed(2)+'%'):'', lcCol=lp?(lc>=0?'color:#d32f2f;':'color:#2e7d32;'):'';
    var tp=s[h.code]&&s[h.code].tp?parseFloat(s[h.code].tp):cost*1.10;
    var sl=s[h.code]&&s[h.code].sl?parseFloat(s[h.code].sl):cost*0.95;
    var tpPct=((tp-cost)/cost*100).toFixed(1), slPct=((sl-cost)/cost*100).toFixed(1);
    html+='<div class="h-item">'+
      '<div class="h-top"><b>'+h.name+'</b><span class="st-code">'+h.code+'</span><span class="h-qty">持仓 '+h.qty+'股</span><button class="h-ops" data-holdop="open" data-code="'+h.code+'">⚙️ 操作</button></div>'+
      '<div class="h-row">成本 <b>¥'+cost.toFixed(2)+'</b> · 现价 <b class="price" data-code="'+h.code+'">'+(lp?'¥'+lp.toFixed(2):'--')+'</b> <span class="chg" data-code="'+h.code+'" style="'+lcCol+'">'+lcTxt+'</span></div>'+
      '<div class="h-row">📈 上涨预估：<b style="color:#2e7d32;" data-edit="'+h.code+':tp" title="点击修改">¥'+tp.toFixed(2)+'（+'+tpPct+'%）</b></div>'+
      '<div class="h-row">🛡 跌停峰值：<b style="color:#c62828;" data-edit="'+h.code+':sl" title="点击修改">¥'+sl.toFixed(2)+'（'+slPct+'%）</b></div>'+
      '<div class="h-pnl" id="pnl_'+h.code+'">--</div>'+
      (function(_a,_c,_q,_lp){
        var r='';
        if(_a){ if(_a.advice==='SELL') r='<div class="h-advice" style="color:#c62828;">🚨 操作建议：抛售（['+_a.rule+']'+_a.why+'）</div>';
                else if(_a.advice==='ADD') r='<div class="h-advice" style="color:#2e7d32;">➕ 操作建议：加仓（['+_a.rule+']'+_a.why+'）</div>'; }
        else if(_lp&&_c&&(_lp-_c)*_q<0){ r='<div class="h-advice" style="color:#2e7d32;">📌 操作建议：持有观望，守住止损线¥'+(_c*0.95).toFixed(2)+'</div>'; }
        return r;
      })(holdAdviceFor(h), cost, h.qty, lp)+'</div>';
  });
  el.innerHTML=html;
  _priceMap=null;
}
function updatePnL(){
  var hs=getHoldings();
  hs.forEach(function(h){
    var pe=document.getElementById('pnl_'+h.code);
    if(!pe) return;
    var p=_lastPrice[h.code]||0;
    if(!p||isNaN(p)) return;
    var cost=h.cost/h.qty;
    var pnl=(p-cost)*h.qty-h.fee;
    var pct=(p-cost)/cost*100;
    pe.innerHTML='浮动盈亏 <b style="color:'+(pnl>=0?'#d4a017':'#2e7d32')+';font-weight:800;">'+(pnl>=0?'+':'')+pnl.toFixed(2)+'元</b>（<b style="color:'+(pnl>=0?'#c62828':'#2e7d32')+';">'+(pct>=0?'+':'')+pct.toFixed(2)+'%</b>） · 累计费用 ¥'+h.fee.toFixed(2);
  });
}
function monitorAlerts(){
  var hs=getHoldings(); if(!hs.length) return;
  var s=loadHS(), alerts=loadAlerts(), now=Date.now(), hits=[];
  hs.forEach(function(h){
    var p=_lastPrice[h.code]||0;
    if(!p||isNaN(p)) return;
    var cost=h.cost/h.qty;
    var sl=s[h.code]&&s[h.code].sl?parseFloat(s[h.code].sl):cost*0.95;
    var tp=s[h.code]&&s[h.code].tp?parseFloat(s[h.code].tp):cost*1.10;
    var line=sl*1.02;
    var a=alerts[h.code]||{};
    if(p<=line && !(a.sl && now-a.sl<1800000)){
      hits.push({code:h.code,name:h.name,price:p,sl:sl,line:line,tp:tp});
      a.sl=now; alerts[h.code]=a;
    }
    var pe=document.getElementById('pnl_'+h.code);
    if(pe&&p>=tp&&!(a.tp && now-a.tp<1800000)){ pe.innerHTML='🎯 已到上涨预估（¥'+tp.toFixed(2)+'），考虑止盈落袋'; a.tp=now; alerts[h.code]=a; }
  });
  if(hits.length){ saveAlerts(alerts); fireAlert(hits); }
}
function fireAlert(hits){
  var bar=document.getElementById('alertBar');
  var msg=hits.map(function(x){ return x.name+' 现价¥'+x.price.toFixed(2)+' 已逼近跌停峰值¥'+x.sl.toFixed(2)+'（距止损2%内）'; }).join('；');
  if(bar){ bar.innerHTML='🚨 <b>预警：</b>'+msg+' <button data-close-alert="1">关闭</button>'; bar.style.display='block'; }
  try{
    if(Notification&&Notification.permission==='granted'){
      new Notification('⚠️ 跌停峰值预警', {body:msg});
    }
  }catch(e){}
  try{
    var ac=new (window.AudioContext||window.webkitAudioContext)();
    var o=ac.createOscillator(), g=ac.createGain();
    o.connect(g); g.connect(ac.destination); o.frequency.value=880;
    g.gain.setValueAtTime(0.3, ac.currentTime); g.gain.exponentialRampToValueAtTime(0.001, ac.currentTime+0.6);
    o.start(); o.stop(ac.currentTime+0.6);
  }catch(e){}
}
function enableNotify(){
  if(!('Notification' in window)){ uiAlert('当前浏览器不支持通知，已用页面内红色预警代替'); return; }
  Notification.requestPermission().then(function(p){
    if(p==='granted') uiAlert('✅ 预警通知已开启：价格跌近跌停峰值时会在通知栏提醒（页面需保持打开）');
    else if(p==='denied') uiAlert('❌ 通知被拒绝，仍会用页面内红色预警');
    else uiAlert('通知权限未确认，仍会用页面内红色预警');
  }).catch(function(){ uiAlert('请求通知权限失败，仍会用页面内红色预警'); });
}
document.addEventListener('click', function(e){
  var t=e.target;
  if(t&&t.getAttribute){
    var ed=t.getAttribute('data-edit');
    if(ed){ var p=ed.split(':'); editLine(p[0], p[1]); return; }
    if(t.getAttribute('data-close-alert')){ var bar=document.getElementById('alertBar'); if(bar) bar.style.display='none'; return; }
    if(t.getAttribute('data-close-ops')){ closeHoldOps(); return; }
    var ho=t.getAttribute('data-holdop');
    if(ho){ var hc=t.getAttribute('data-code'); if(ho==='open'){ openHoldOps(hc); return; } doHoldOp(hc, t.getAttribute('data-name'), parseFloat(t.getAttribute('data-price')||'0'), ho); return; }
    var wc=t.getAttribute('data-watch');
    if(wc){ showStockDetail(wc, t.getAttribute('data-name')); return; }
    if(t.getAttribute('data-stbuy')){ var bc=t.getAttribute('data-code'), bn=t.getAttribute('data-name'); var rp=_lastPrice[bc]||parseFloat(t.getAttribute('data-price')||'0'); buyWithBudget(bc,bn,rp); return; }
    if(t.getAttribute('data-stsell')){ openHoldOps(t.getAttribute('data-code')); return; }
  }
});
// ===== 未持股波动推荐：历史买卖/今日推荐标的，涨跌幅≥4%异动推荐 =====
function collectWatchCodes(){
  var codes={};
  var trades=getTrades();
  trades.forEach(function(t){ if(t.code&&!codes[t.code]) codes[t.code]=t.name||t.code; });
  document.querySelectorAll('.st-top').forEach(function(top){
    var b=top.querySelector('b'), sc=top.querySelector('.st-code');
    if(b&&sc){ var c=(sc.textContent||'').trim(); if(c&&!codes[c]) codes[c]=b.textContent||c; }
  });
  var hs=getHoldings();
  hs.forEach(function(h){ delete codes[h.code]; });
  var out=[];
  for(var c in codes){ out.push({code:c,name:codes[c]}); }
  return out.slice(0,20);
}
function refreshWatch(){
  var el=document.getElementById('watchList'); if(!el) return;
  var list=collectWatchCodes();
  if(!list.length){ el.innerHTML='<div class="h-empty">暂无关注标的</div>'; return; }
  var html=''; var any=false;
  list.forEach(function(x){
    var price=_lastPrice[x.code]; if(!price||isNaN(price)) return;
    var chg=_lastChg[x.code]||0; if(isNaN(chg)||Math.abs(chg)<4) return;
    any=true;
    var up=chg>=0;
    var vol=_lastVol[x.code]||0;
    var tip=up?'放量上攻，短线可关注':(vol>=1.5?'放量回调，暂观望':'回调波动，暂观望');
    var col=up?'#c62828':'#2e7d32';
    html+='<div class="w-item" data-watch="'+x.code+'" data-name="'+x.name+'">'+
      '<b>'+x.name+'</b><span class="st-code">'+x.code+'</span>'+
      '<span class="w-chg" style="color:'+col+';">'+(up?'📈 大涨':'📉 大跌')+' '+price.toFixed(2)+'（'+(up?'+':'')+chg.toFixed(2)+'%）</span>'+
      '<span class="w-tip">'+tip+'</span></div>';
  });
  el.innerHTML=any?html:'<div class="h-empty">暂无波动异动：关注标的涨跌幅≥4%时自动推荐</div>';
}
// ===== 交易时段检测：闭市时整页灰色（持仓卡保持彩色） =====
function isTradingTime(d){
  d=d||new Date();
  var day=d.getDay();
  if(day===0||day===6) return false;               // 周末闭市
  var m=d.getHours()*60+d.getMinutes();
  return (m>=570&&m<=690)||(m>=780&&m<=900);       // 09:30-11:30 / 13:00-15:00
}
function updateMarketStatus(d){
  var open=isTradingTime(d);
  var el=document.getElementById('marketStatus');
  // 暂时去除灰界面：闭市时也不加灰色滤镜、不显示闭市横幅（保留行情逻辑）
  document.body.classList.remove('market-closed');
  if(el){ el.style.display='none'; }
}
setTimeout(updateMarketStatus, 500);
setInterval(updateMarketStatus, 30000); // 每30秒刷新闭市状态
function updateStWarn(){
  document.querySelectorAll('.st-warn[data-stwarn]').forEach(function(el){
    var code=el.getAttribute('data-stwarn');
    var tp=parseFloat(el.getAttribute('data-tp')||'0'), sl=parseFloat(el.getAttribute('data-sl')||'0');
    var p=_lastPrice[code], chg=_lastChg[code];
    var m='';
    if(!p||isNaN(p)){ return; }
    if(tp&&p>=tp) m='🚨 已达涨值巅峰，建议落袋/勿追高';
    else if(sl&&p<=sl) m='🔻 已破跌停锋线，注意止损';
    else if(!isNaN(chg)&&chg>=9.5) m='🚀 强势逼近涨停，追高风险大';
    else if(tp&&sl&&p>sl&&p<tp) m='✅ 区间内，可按建议买入区间分批';
    el.innerHTML=m;
    el.style.color=(m.indexOf('🚨')===0||m.indexOf('🔻')===0)?'#c62828':'#2e7d32';
  });
}
function holdTimer(){ refreshHoldings(); updatePnL(); monitorAlerts(); refreshWatch(); refreshRealtime(); updateStWarn(); refreshFundPanel(); }
setInterval(holdTimer, 2000); // 持仓信息每2秒更新
setTimeout(holdTimer, 500);  // 页面加载后先刷一次
// ===== 今日短线推荐目录：每15分钟fetch最新JSON重渲染（确保可买+短线高增长） =====
function escHtml(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function hasHolding(code){
  var q=0;
  getTrades().forEach(function(t){ if(t.code===code){ q+=t.type==='buy'?t.qty:-t.qty; } });
  return q>0;
}
function renderShortTerm(j){
  var dateTxt=escHtml(j.date||''), tsTxt=escHtml(j.ts||'');
  var html='<div class="card short-term"><div class="card-top"><div class="name">📈 今日短线推荐</div><div class="st-date">'+dateTxt+' 目录更新 '+(tsTxt?tsTxt:'最新')+' · 每15分钟刷新</div></div>';
  html+='<div class="st-summary"><b>市场综述：</b>'+escHtml(j.market_summary||'')+'</div>';
  var budget=getBudget();
  // 短期优选置顶：按“预判1-2天盈利空间”降序（涨值巅峰线相对现价空间），空间相同按当日涨幅
  var arr=(j.stocks||[]).slice();
  arr.forEach(function(s){
    var p=parseFloat(s.price), tp=parseFloat(s.take_profit);
    s._sp=(!isNaN(p)&&!isNaN(tp)&&p>0)?((tp-p)/p*100):0;
  });
  arr.sort(function(a,b){ return (b._sp-a._sp)||((parseFloat(b.chg)||0)-(parseFloat(a.chg)||0)); });
  arr.forEach(function(s){
    var p=parseFloat(s.price), chg=parseFloat(s.chg);
    var up=chg>=0, scol=up?'#c62828':'#2e7d32';
    var canBuy=!isNaN(p)&&p*100<=budget;
    var badge=canBuy?'<span class="st-badge">✓ 可买</span>':'<span class="st-badge" style="background:#ef6c00;">⚠️ 资金不足</span>';
    var priceTxt=isNaN(p)?'--':'¥'+p.toFixed(2);
    var chgTxt=isNaN(chg)?'--':((up?'+':'')+chg.toFixed(2)+'%');
    var held=hasHolding(s.code);
    var hot=(arr.indexOf(s)===0)?'<span class="st-hot-badge">🔥 今日优选·1-2天看涨</span>':'';
    var spTxt=isNaN(s._sp)?'':'<span class="st-space" style="color:'+(s._sp>=0?'#c62828':'#2e7d32')+';">预判1-2天空间 '+(s._sp>=0?'+':'')+s._sp.toFixed(1)+'%</span>';
    html+='<div class="st-item'+(arr.indexOf(s)===0?' st-hot':'')+'"><div class="st-top"><b style="color:#1a237e;">'+escHtml(s.name)+'</b>'+
      badge+hot+'<span class="st-code">'+escHtml(s.code)+'</span>'+
      spTxt+
      '<span class="st-price price" data-code="'+escHtml(s.code)+'" style="color:'+scol+';">'+priceTxt+'</span> '+
      '<span class="st-chg chg" data-code="'+escHtml(s.code)+'">'+chgTxt+'</span></div>'+
      '<div class="st-reason">✅ 可买理由：'+escHtml(s.reason||'--')+'</div>'+
      '<div class="st-qty">📌 建议买入股数：<b style="color:#c62828;">'+escHtml(s.qty||'--')+'</b></div>'+
      '<div class="st-line">📈 涨值巅峰线：<b style="color:#2e7d32;">'+escHtml(s.take_profit||'--')+'元</b> · 🛡 跌停锋线：<b style="color:#c62828;">'+escHtml(s.stop_loss||'--')+'元</b></div>'+
      '<div class="st-ref">买入区间 <b>'+escHtml(s.buy_range||'--')+'</b> · 止损 <b>'+escHtml(s.stop_loss||'--')+'</b> · 止盈 <b>'+escHtml(s.take_profit||'--')+'</b></div>'+
      '<div class="st-warn" data-stwarn="'+escHtml(s.code)+'" data-tp="'+escHtml(s.take_profit||'0')+'" data-sl="'+escHtml(s.stop_loss||'0')+'"></div>'+
      '<div class="st-ops">'+
      '<button class="st-buy" data-stbuy="1" data-code="'+escHtml(s.code)+'" data-name="'+escHtml(s.name)+'" data-price="'+(isNaN(p)?'0':p)+'" '+(held?'style="opacity:0.5;" title="已持有，可加仓"':'')+'>'+(held?'➕ 已持有·加仓':'📥 买入')+'</button>'+
      (held?'<button class="st-sell" data-stsell="1" data-code="'+escHtml(s.code)+'">💰 卖出</button>':'')+
      '</div></div>';
  });
  html+='<div class="st-note">⚠️ '+escHtml(j.note||'模拟交易仅记录本地，不构成投资建议；短线风险高，控制仓位')+'</div></div>';
  return html;
}
function fetchStRec(){
  // 云端定时任务只PUT仓库JSON（不触发构建），raw 即时反映最新目录；同源仅作兜底
  return fetch('https://raw.githubusercontent.com/hugoyeswood/stock-picker-v3/main/short_term_rec.json?v='+Date.now())
    .then(function(r){ return r.ok?r.json():null; })
    .catch(function(){
      return fetch('short_term_rec.json?v='+Date.now()).then(function(r){ return r.ok?r.json():null; }).catch(function(){ return null; });
    });
}
function refreshShortTerm(){
  var el=document.getElementById('stRecContainer'); if(!el) return;
  fetchStRec().then(function(j){
    if(!j||!j.stocks) return;
    var d=new Date();
    var todayStr=d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2);
    if(j.date&&j.date!==todayStr) return; // 只渲染当天目录
    el.innerHTML=renderShortTerm(j);
    _priceMap=null; // 新渲染元素需重新收集，价格继续每秒刷新
    updateStWarn();
  });
}
setTimeout(refreshShortTerm, 2500);   // 页面加载后先拉一次
setInterval(refreshShortTerm, 900000); // 每15分钟更新推荐目录
// ===== 持仓着重关注：每小时抛售/加仓提示 =====
// 信号矩阵（对每只持仓股，现价用每秒实时缓存 _lastPrice）：
//   抛售SELL：现价≤止损线×1.02（逼近跌停峰值）| 现价≥止盈目标（涨值巅峰落袋）| 当日涨幅≥9.5%（接近涨停锁利）
//            | 当日涨幅≥5%且累计盈利≥8%（冲高锁利）| 当日跌幅≤-5%（趋势转弱止损）
//   加仓ADD ：累计浮亏≤-4%且当日温和回调-3%~0%（未破止损，分批摊薄）| 现价在成本±2%内且当日温和上行0~3%（启动在即）
//            | 现价仍低于成本但当日反弹+2%~+6%（反弹确认）
//   其余HOLD：持有观望
function holdAdviceFor(h){
  var p=_lastPrice[h.code]||0; if(!p||isNaN(p)) return null;
  var cost=h.cost/h.qty;
  var s=loadHS()[h.code]||{};
  var sl=s.sl?parseFloat(s.sl):cost*0.95;
  var tp=s.tp?parseFloat(s.tp):cost*1.10;
  var chg=_lastChg[h.code]||0; if(isNaN(chg)) chg=0;
  var vr=_lastVol[h.code]||0; var tr=_lastTr[h.code]||0; var amp=_lastAmp[h.code]||0;
  var high=_lastHigh[h.code]||0; var low=_lastLow[h.code]||0;
  var pnlPct=(p-cost)/cost*100;
  var pullback=high>0?(high-p)/high*100:0; // 距当日最高回落%
  var advice=null, why='', rule='';
  // —— 抛售 SELL：利弗莫尔（截断亏损/顶部派发/强弱转换）· 欧奈尔（目标止盈/赶顶锁利） ——
  if(p<=sl*1.02){ advice='SELL'; rule='利弗莫尔·截断亏损'; why='现价¥'+p.toFixed(2)+'破位止损线¥'+sl.toFixed(2)+'，"截断亏损，让利润奔跑"，立即离场'; }
  else if(p>=tp){ advice='SELL'; rule='欧奈尔·目标止盈'; why='已达上涨预估¥'+tp.toFixed(2)+'，欧奈尔"+20%止盈"纪律，落袋为安'; }
  else if(chg>=3&&chg<=7&&vr>=2.5&&amp>=8&&pullback>=3){ advice='SELL'; rule='利弗莫尔·顶部派发'; why='放量滞涨：涨'+chg.toFixed(2)+'%但量比'+vr.toFixed(1)+'、已从最高回落'+pullback.toFixed(1)+'%，主力派发，抛售'; }
  else if(pnlPct>=15&&chg>=6){ advice='SELL'; rule='欧奈尔·赶顶锁利'; why='累计盈利'+pnlPct.toFixed(1)+'%且加速赶顶（+'+chg.toFixed(2)+'%），锁定利润'; }
  else if(chg<=-5||pnlPct<=-8){ advice='SELL'; rule='利弗莫尔·强弱转换'; why='今日'+chg.toFixed(2)+'%由强转弱'+(pnlPct<=-8?('、累计浮亏'+pnlPct.toFixed(1)+'%'):'')+'，趋势转空，离场观望'; }
  // —— 加仓 ADD：欧奈尔（突破回踩）· 利弗莫尔（关键点企稳）· 海龟（缩量回调）· 幽灵（盈利加码） ——
  else if(pnlPct>=-2&&pnlPct<=2&&chg>=1&&chg<=4&&vr>=1.5){ advice='ADD'; rule='欧奈尔·突破回踩'; why='成本附近（'+pnlPct.toFixed(1)+'%）放量上攻（量比'+vr.toFixed(1)+'，+'+chg.toFixed(2)+'%），突破回踩买点，加仓'; }
  else if(p>sl*1.02&&pnlPct<=-4&&pnlPct>=-6&&chg>=-3&&chg<=0&&vr<1.5){ advice='ADD'; rule='利弗莫尔·关键点企稳'; why='回调至成本下方'+(-pnlPct).toFixed(1)+'%未破止损、缩量企稳（量比'+vr.toFixed(1)+'），关键点支撑，分批加仓'; }
  else if(pnlPct<=-2&&pnlPct>=-5&&amp>0&&amp<5&&chg>=-2&&chg<=1){ advice='ADD'; rule='海龟·缩量回调到位'; why='缩量回调到位（振幅'+amp.toFixed(1)+'%），趋势中回踩，加仓'; }
  else if(pnlPct>=2&&pnlPct<=8&&chg>=1&&chg<=5&&vr>=1.5&&vr<=3){ advice='ADD'; rule='幽灵·盈利加码'; why='趋势确认盈利+'+pnlPct.toFixed(1)+'%、量价配合（量比'+vr.toFixed(1)+'），金字塔加仓'; }
  return advice?{code:h.code,name:h.name,price:p,cost:cost,sl:sl,tp:tp,pnlPct:pnlPct,chg:chg,vr:vr,tr:tr,amp:amp,high:high,low:low,advice:advice,rule:rule,why:why}:null;
}
function renderHoldAdvice(){
  var hs=getHoldings(); if(!hs.length) return;
  hs.forEach(function(h){
    var a=holdAdviceFor(h); if(!a) return;
    var item=document.getElementById('pnl_'+h.code); if(!item) return;
    var wrap=item.parentNode; if(!wrap) return;
    var old=wrap.querySelector('.h-adv'); if(old) old.parentNode.removeChild(old);
    var color=a.advice==='SELL'?'#c62828':'#2e7d32';
    var tag=a.advice==='SELL'?'🚨 建议抛售':'➕ 建议加仓';
    var div=document.createElement('div');
    div.className='h-adv';
    div.style.cssText='margin-top:6px;padding:4px 8px;border-radius:4px;font-size:12px;color:#fff;background:'+color+';';
    div.textContent=tag+'：['+a.rule+'] '+a.why;
    wrap.appendChild(div);
  });
}
function checkHourlyAdvice(){
  var hs=getHoldings(); if(!hs.length) return;
  var sell=[], add=[];
  hs.forEach(function(h){
    var a=holdAdviceFor(h);
    if(a){ if(a.advice==='SELL') sell.push(a); else if(a.advice==='ADD') add.push(a); }
  });
  if(!sell.length&&!add.length) return;
  var now=new Date();
  var key=String(now.getFullYear())+('0'+(now.getMonth()+1)).slice(-2)+('0'+now.getDate()).slice(-2)+('0'+now.getHours()).slice(-2);
  var last=''; try{ last=localStorage.getItem('myAdviceHour')||''; }catch(e){}
  if(last===key) return; // 同一小时只提示一次
  try{ localStorage.setItem('myAdviceHour', key); }catch(e){}
  var msg='';
  if(sell.length) msg+='🚨 建议抛售：'+sell.map(function(a){ return a.name+'（['+a.rule+']'+a.why+'）'; }).join('；')+'。';
  if(add.length) msg+='➕ 建议加仓：'+add.map(function(a){ return a.name+'（['+a.rule+']'+a.why+'）'; }).join('；')+'。';
  var bar=document.getElementById('alertBar');
  if(bar){ bar.innerHTML='🕐 <b>每小时持仓提示：</b>'+msg+' <button data-close-alert="1">关闭</button>'; bar.style.display='block'; }
  try{
    if(Notification&&Notification.permission==='granted'){ new Notification('持仓抛售/加仓提示', {body:msg}); }
  }catch(e){}
}
setInterval(checkHourlyAdvice, 60000); // 每分钟检查，命中新小时即汇总提示（每小时最多一次）
// ===== 持仓操作：加仓/减仓/清仓/卖出盈利点 =====
function doHoldOp(code,name,price,op){
  if(op==='buyMore') buyMore(code,name,price);
  else if(op==='sellPart') sellPart(code,name,price);
  else if(op==='sellAll') sellAll(code,name,price);
}
function openHoldOps(code){
  var hs=getHoldings(), one=null;
  hs.forEach(function(x){ if(x.code===code) one=x; });
  if(!one){ uiAlert('未找到该持仓'); return; }
  var el=document.querySelector('.price[data-code="'+code+'"]');
  var price=el?parseFloat(el.textContent.replace('¥','')):0;
  var cost=one.cost/one.qty;
  var s=loadHS()[code]||{};
  var tp=s.tp?parseFloat(s.tp):cost*1.10;
  var pnl=(price-cost)*one.qty-one.fee;
  var pnlTxt=price?(pnl>=0?'+':'')+pnl.toFixed(2)+'元（'+((price-cost)/cost*100>=0?'+':'')+((price-cost)/cost*100).toFixed(2)+'%）':'--';
  var tpPct=((tp-cost)/cost*100).toFixed(1);
  var profitAtTp=(tp-cost)*one.qty;
  var html='<div style="position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;">'+
    '<div style="background:#fff;border-radius:12px;padding:20px;width:90%;max-width:400px;max-height:80vh;overflow-y:auto;">'+
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">'+
    '<div><h2 style="margin:0;font-size:18px;">📊 '+one.name+'</h2><div style="color:#666;font-size:12px;">'+code+'</div></div>'+
    '<button data-close-ops="1" style="background:#eee;border:none;border-radius:50%;width:32px;height:32px;cursor:pointer;font-size:16px;">×</button></div>'+
    '<div style="font-size:13px;background:#f5f5f5;border-radius:8px;padding:10px;margin-bottom:12px;">'+
    '持仓 <b>'+one.qty+'股</b> · 买入成本 <b>¥'+cost.toFixed(2)+'</b><br>现价 <b style="color:#1a237e;">'+(price?'¥'+price.toFixed(2):'--')+'</b> · 浮动盈亏 <b style="color:'+(pnl>=0?'#c62828':'#2e7d32')+';">'+pnlTxt+'</b></div>'+
    '<div style="font-size:13px;background:#e8f5e9;border-radius:8px;padding:10px;margin-bottom:14px;">'+
    '💰 卖出盈利点：<b style="color:#2e7d32;cursor:pointer;border-bottom:1px dashed #bbb;" data-edit="'+code+':tp">¥'+tp.toFixed(2)+'（+'+tpPct+'%）</b>（点击修改）<br>'+
    '达此价全部卖出预计盈利：<b>¥'+profitAtTp.toFixed(2)+'</b>（未扣费用）</div>'+
    '<div style="display:flex;gap:8px;flex-wrap:wrap;">'+
    '<button data-holdop="buyMore" data-code="'+code+'" data-name="'+one.name+'" data-price="'+price+'" style="flex:1;padding:12px;background:#c62828;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;">➕ 加仓</button>'+
    '<button data-holdop="sellPart" data-code="'+code+'" data-name="'+one.name+'" data-price="'+price+'" style="flex:1;padding:12px;background:#ff7043;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;">➖ 减仓</button></div>'+
    '<div style="margin-top:8px;"><button data-holdop="sellAll" data-code="'+code+'" data-name="'+one.name+'" data-price="'+price+'" style="width:100%;padding:12px;background:#2e7d32;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;">🗑 清仓（全部卖出）</button></div>'+
    '<div style="margin-top:12px;font-size:11px;color:#999;text-align:center;">⚠️ 模拟交易仅记录本地，不构成投资建议</div>'+
    '</div></div>';
  var m=document.getElementById('holdOpsModal');
  if(!m){ m=document.createElement('div'); m.id='holdOpsModal'; document.body.appendChild(m); }
  m.innerHTML=html; m.style.display='block';
}
function closeHoldOps(){ var m=document.getElementById('holdOpsModal'); if(m) m.style.display='none'; }
function buyMore(code,name,price){
  if(!price){ uiAlert('暂无现价，请稍后重试'); return; }
  var budget=getBudget();
  var maxQty=Math.floor(budget/price/100)*100;
  if(maxQty<100){ uiAlert('可用资金不足！可用 ¥'+budget.toFixed(2)+'，现价¥'+price.toFixed(2)+'，1手需¥'+(price*100).toFixed(2)); return; }
  uiPrompt('加仓数量（股），可用资金最多可买 '+maxQty+'股：', maxQty, function(qi){
    if(!qi||isNaN(qi)||parseInt(qi)<=0) return;
    var q=Math.floor(parseInt(qi)/100)*100; if(q<100){ uiAlert('A股最少1手=100股'); return; }
    var amount=price*q, fee=calcFee(amount,'buy'), total=amount+fee;
    if(total>budget){ uiAlert('资金不足：需¥'+total.toFixed(2)+'，可用¥'+budget.toFixed(2)); return; }
    var trades=getTrades();
    trades.push({type:'buy',code:code,name:name,price:price,qty:q,amount:amount,fee:fee,time:new Date().toLocaleString('zh-CN'),auto:true});
    saveTrades(trades);
    setBudget(budget-total);
    uiAlert('✅ 加仓成功：'+name+' '+q+'股 @ ¥'+price.toFixed(2)+'\\n支出：¥'+total.toFixed(2)+'（含费用¥'+fee.toFixed(2)+'）\\n剩余资金：¥'+(budget-total).toFixed(2));
    closeHoldOps(); refreshHoldings();
  });
}
function sellPart(code,name,price){
  var hs=getHoldings(), one=null;
  hs.forEach(function(x){ if(x.code===code) one=x; });
  if(!one) return;
  if(!price){ uiAlert('暂无现价，请稍后重试'); return; }
  uiPrompt('减仓数量（股），当前持仓 '+one.qty+'股：', one.qty, function(qi){
    if(!qi||isNaN(qi)||parseInt(qi)<=0) return;
    var q=parseInt(qi); if(q>one.qty){ uiAlert('超过持仓数量！当前持仓 '+one.qty+'股'); return; }
    var amount=price*q, fee=calcFee(amount,'sell');
    var trades=getTrades();
    trades.push({type:'sell',code:code,name:name,price:price,qty:q,amount:amount,fee:fee,time:new Date().toLocaleString('zh-CN')});
    saveTrades(trades);
    setBudget(getBudget()+amount-fee);
    uiAlert('✅ 减仓成功：'+name+' '+q+'股 @ ¥'+price.toFixed(2)+'\\n回款：¥'+(amount-fee).toFixed(2)+'（扣费¥'+fee.toFixed(2)+'）');
    closeHoldOps(); refreshHoldings();
  });
}
function sellAll(code,name,price){
  var hs=getHoldings(), one=null;
  hs.forEach(function(x){ if(x.code===code) one=x; });
  if(!one) return;
  if(!price){ uiAlert('暂无现价，请稍后重试'); return; }
  uiPrompt('确认清仓全部卖出 '+name+' '+one.qty+'股？', '', function(v){
    if(!v || v.trim()==='') return;
    var q=one.qty, amount=price*q, fee=calcFee(amount,'sell');
    var trades=getTrades();
    trades.push({type:'sell',code:code,name:name,price:price,qty:q,amount:amount,fee:fee,time:new Date().toLocaleString('zh-CN')});
    saveTrades(trades);
    setBudget(getBudget()+amount-fee);
    uiAlert('✅ 已清仓：'+name+' '+q+'股 @ ¥'+price.toFixed(2)+'\\n回款：¥'+(amount-fee).toFixed(2)+'（扣费¥'+fee.toFixed(2)+'）');
    closeHoldOps(); refreshHoldings();
  });
}
"""

APP_JS_UI = """// ===== V3 融合版：Tab 分区导航 + 全局错误兜底 + 按钮可用性自检 + V5大师推荐池 =====
function switchTab(id){
  var tabs=document.querySelectorAll('.tab-page');
  for(var i=0;i<tabs.length;i++){ tabs[i].className = tabs[i].id===id ? 'tab-page active' : 'tab-page'; }
  var btns=document.querySelectorAll('.tab-btn');
  for(var j=0;j<btns.length;j++){ var b=btns[j]; b.className = b.getAttribute('data-tab')===id ? 'tab-btn active' : 'tab-btn'; }
  try{ if(id==='tabHold'){ refreshHoldings(); updatePnL(); } if(id==='tabFund'){ refreshFundPanel(); } if(id==='tabCand'){ _priceMap=null; refreshRealtime(); } }catch(e){}
}
// ===== V5 大师推荐池 candidates.json：实时校验 TOP15 并更新统计（ES5） =====
function loadCandidatePool(){
  fetch('candidates.json?v='+Date.now())
    .then(function(r){ return r.ok ? r.json() : null; })
    .catch(function(){ return null; })
    .then(function(j){
      if(!j || !j.candidates || !j.candidates.length) return;
      var el=document.getElementById('poolMeta');
      if(!el) return;
      var n=0, best=0;
      j.candidates.forEach(function(c){ if(c.score>best) best=c.score; if(c.score>=7) n++; });
      el.innerHTML='<div style="font-size:11px;color:#666;background:#fff8e1;border-radius:6px;padding:6px 10px;margin-bottom:8px;">'+
        '🧠 大师推荐池 <b>TOP'+j.candidates.length+'</b> · '+escHtml(j.date||'')+' '+escHtml(j.updated||'')+' · '+
        '🏆最优(≥7分)<b style="color:#c62828;">'+n+'只</b> · 最高分<b style="color:#1a237e;">'+best+'分</b></div>';
    });
}
setTimeout(loadCandidatePool, 2000);
(function(){
  // Tab 点击委托（ES5，兼容老内核）
  document.addEventListener('click', function(e){
    var t=e.target;
    while(t && t!==document.body){
      if(t.getAttribute && t.getAttribute('data-tab')){ switchTab(t.getAttribute('data-tab')); return; }
      t=t.parentNode;
    }
  });
  // 全局错误兜底：JS 异常不再静默，顶部红条提示（不阻断功能）
  window.onerror = function(msg, src, line){
    try{
      var bar=document.getElementById('errBanner');
      if(bar){ bar.style.display='block';
        bar.innerHTML='⚠️ 页面运行提示：'+String(msg).slice(0,80)+'（行'+line+'）· 数据与功能仍在运行';
        setTimeout(function(){ bar.style.display='none'; }, 8000);
      }
    }catch(e){}
    return false;
  };
  // 按钮可用性自检：遍历所有带 onclick / data-* 的可点元素，检查对应全局函数是否存在
  function selfCheckButtons(){
    var missing=[];
    var all=document.querySelectorAll('[onclick]');
    for(var i=0;i<all.length;i++){
      var oc=(all[i].getAttribute('onclick')||'').match(/^\\s*(\\w+)\\s*\\(/);
      if(oc && typeof window[oc[1]]!=='function'){ missing.push(oc[1]); }
    }
    var checks=['buyWithBudget','recordTrade','searchStocks','showStockDetail','toggleChart','openHoldOps','editPrincipal','editBudget','exportBackup','importBackup','showMonthReport','enableNotify','refreshShortTerm','refreshHoldings','refreshRealtime','refreshFundPanel'];
    for(var k=0;k<checks.length;k++){
      if(typeof window[checks[k]]!=='function') missing.push(checks[k]);
    }
    var el=document.getElementById('selfCheck');
    if(el){
      el.textContent = missing.length ? '⚠️ 自检发现缺失函数：'+missing.join('、') : '✅ 全部按钮链路自检通过（'+checks.length+'项）';
      el.style.color = missing.length ? '#c62828' : '#2e7d32';
    }
  }
  setTimeout(selfCheckButtons, 1500);
})();
"""



html_doc = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#1a237e">
<link rel="manifest" href="manifest.json">
<link rel="icon" href="icons/icon-192.png" sizes="192x192">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<title>今日候选清单 · 大师逻辑优选 V3（融合版）</title>
<meta property="og:title" content="股票短线优选 · 利弗莫尔/欧奈尔大师逻辑 V3">
<meta property="og:description" content="大师量化打分引擎、TOP15推荐池、每日短线推荐、涨值巅峰线预判、大师级买卖抛售逻辑、模拟交易。打开即用，微信内也可直接访问。">
<meta property="og:image" content="https://hugoyeswood.github.io/stock-picker-v3/icons/icon-512.png">
<meta property="og:url" content="https://hugoyeswood.github.io/stock-picker-v3/">
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Microsoft YaHei", system-ui, sans-serif; background:#f0f2f5;
         max-width:640px; margin:0 auto; padding:14px; }}
  h1 {{ font-size:20px; color:#1a237e; margin-bottom:4px; }}
  .sub {{ font-size:12px; color:#777; margin-bottom:12px; }}
  .tip {{ background:#fff8e1; border-left:4px solid #f9a825; border-radius:6px;
         padding:10px 12px; font-size:13px; color:#5d4a00; margin-bottom:14px; }}
  .stat {{ display:flex; gap:8px; margin-bottom:14px; }}
  .stat div {{ flex:1; background:#fff; border-radius:8px; padding:10px; text-align:center;
              box-shadow:0 1px 3px rgba(0,0,0,.06); }}
  .stat b {{ display:block; font-size:20px; color:#1a237e; }}
  .stat span {{ font-size:11px; color:#888; }}
  .gold {{ color:#d4a017; font-weight:800; }}
  .market-badge {{ display:flex; align-items:center; gap:6px; background:#1a237e; color:#ffd54f; border-radius:8px;
         padding:8px 12px; font-size:13px; margin-bottom:14px; }}
  body.market-closed {{ filter:grayscale(0.85); }}
  body.market-closed .card.holdings {{ filter:none; }}
  body.market-closed #stockDetailModal, body.market-closed #holdOpsModal {{ filter:none; }}
  .h-advice {{ margin-top:4px; font-size:12px; }}
  .card {{ background:#fff; border-radius:12px; padding:14px; margin-bottom:12px;
          box-shadow:0 1px 4px rgba(0,0,0,.07); }}
  .card-top {{ display:flex; justify-content:space-between; align-items:flex-start; }}
  .name {{ font-size:17px; font-weight:bold; }}
  .code {{ font-size:11px; color:#999; }}
  .score {{ background:#c62828; color:#fff; border-radius:14px; padding:3px 10px;
           font-weight:bold; font-size:14px; }}
  .state {{ margin-left:6px; border-radius:14px; padding:3px 10px; font-weight:bold; font-size:13px; }}
  .price-line {{ margin:10px 0 6px; display:flex; align-items:baseline; gap:10px; }}
  .price {{ font-size:26px; font-weight:bold; }}
  .chg {{ font-size:16px; font-weight:bold; }}
  .tags {{ display:flex; gap:8px; margin-bottom:8px; flex-wrap:wrap; }}
  .tag {{ font-size:12px; background:#f5f5f5; border-radius:10px; padding:3px 8px; }}
  .meta {{ font-size:12px; color:#666; margin-bottom:8px; }}
  .trade {{ border-radius:8px; padding:8px 10px; margin-bottom:8px; }}
  .t-advice {{ font-size:13px; font-weight:bold; margin-bottom:4px; }}
  .t-row {{ font-size:13px; margin-bottom:4px; }}
  .t-range {{ font-size:13px; margin-bottom:4px; }}
  .t-risk {{ font-size:13px; color:#c62828; background:#ffebee; border-radius:6px;
            padding:5px 8px; margin:6px 0; }}
  .t-confirm {{ font-size:12px; color:#555; background:rgba(255,255,255,.6); border-radius:6px;
               padding:5px 8px; }}
  .why {{ font-size:13px; color:#333; line-height:1.5; margin-bottom:8px; }}
  .hands {{ font-size:13px; color:#1a237e; background:#e8eaf6; border-radius:8px; padding:7px 10px; }}
  .wallet {{ background:#fbfdff; border:1px solid #dbe4f7; }}
  .w-total b {{ font-size:18px; color:#1a237e; }}
  .w-row {{ display:flex; gap:8px; font-size:13px; padding:3px 0; color:#444; }}
  .w-date {{ color:#888; width:120px; }}
  .w-amt {{ font-weight:bold; color:#2e7d32; }}
  .w-note {{ color:#666; flex:1; }}
  .w-hint {{ font-size:12px; color:#888; margin-top:6px; }}
  .foot {{ font-size:11px; color:#999; text-align:center; margin-top:16px; line-height:1.6; }}
{EXTRA_CSS}</style></head><body>
<div id="wxTip" style="display:none;background:#e8f5e9;color:#2e7d32;border:1px solid #a5d6a7;border-radius:8px;padding:8px 10px;font-size:12px;margin-bottom:10px;">📱 已在微信内打开，功能完整可用；如需全屏/横屏体验，点右上角「···」→「在浏览器打开」</div>
<script>
(function(){{ try{{ if(/MicroMessenger/i.test(navigator.userAgent)){{ var t=document.getElementById('wxTip'); if(t) t.style.display='block'; }} }}catch(e){{}} }})();
</script>
<h1>今日候选清单 <span class="ver-badge">大师逻辑 V3</span></h1>
<div class="sub">更新于 {now} · 数据源：{esc(src)} · 当前可用资金 ¥{BUDGET:.2f} <span class="ver-badge">v20261105-V3-ES5·融合V5</span></div>
<div id="errBanner" class="err-banner"></div>
<div id="marketStatus" class="market-badge" style="display:none"></div>
<div class="tip"><b>怎么用：</b>看卡片【状态】——"可买"按【建议买入区间】挂限价单买1手；"等回踩"别追高；"观望"先别碰。<br>
<b>🏆最优/⭐优选/关注</b>=大师量化打分分级（利弗莫尔趋势启动+欧奈尔量价齐升+站上5日线+换手+估值）。<br>
买入后设好<b>止损</b>(跌到就走)和<b>止盈</b>(涨到落袋)。<b>提醒：</b>数据筛选，非投资建议；涨跌有风险，控制仓位。</div>
<div class="tabs" id="mainTabs">
  <button class="tab-btn active" data-tab="tabRec">📈 今日推荐</button>
  <button class="tab-btn" data-tab="tabCand">🔥 候选清单</button>
  <button class="tab-btn" data-tab="tabHold">📊 我的持仓</button>
  <button class="tab-btn" data-tab="tabFund">💰 资金账户</button>
</div>
{SEARCH_HTML}
<div class="tab-page active" id="tabRec">
{short_term_html}
</div>
<div class="tab-page" id="tabCand">
  <div id="poolMeta"></div>
  <div class="stat">
    <div><b>{len(out)}</b><span>今日候选</span></div>
    <div><b>{len(pool)}</b><span>买得起的候选</span></div>
    <div><b>{sum(1 for x in out if x['trade']['state']=='可买')}</b><span>"可买"数量</span></div>
  </div>
  {cards}
</div>
<div class="tab-page" id="tabHold">
{HOLD_HTML}
</div>
<div class="tab-page" id="tabFund">
{wallet_html}
</div>
<div class="foot">状态依据：现价 vs 20日均线、离5日均线远近；买入区间=现价与MA5之间。<br>
资金账户：本地 python add_fund.py 转入资金后重新部署 · 添加到主屏幕即可像App一样用 · 每天收盘后自动更新<br>
<span id="selfCheck">自检中...</span></div>
<script>
{APP_JS}
{APP_JS_UI}
{APP_JS_EXTRA}
{APP_JS_ADVICE}
{APP_JS_HOLD}
if ('serviceWorker' in navigator) {{ navigator.serviceWorker.register('./service-worker.js'); }}
</script>
</body></html>"""

with open("index.html", "w", encoding="utf-8") as f:
    f.write(html_doc)

print("\n完成！生成：index.html（手机页）+ data.json（数据备份）+ candidates.json（V5 TOP15推荐池）")
print(f"更新于 {now}，数据源 {src}，候选 {len(out)} 只，可用资金 ¥{BUDGET:.2f}")
