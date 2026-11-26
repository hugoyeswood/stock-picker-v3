#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端止损守卫 (cloud guard)

目的：手机页面关闭时，前端的 autoTick 会完全停摆，止盈止损失效。
本脚本在云主机上按分钟级独立运行，套用与前端 autoTick 完全相同的卖出判定口径，
把卖单直接写进 ntfy 云端账本，手机开不开都不影响止损。

设计约束：
- 只做卖出，不做买入（买入仍由手机端负责，避免重复建仓）
- 卖出判定与前端顺序一致：止盈 → 止损 → 尾盘锁利 → 微利锁 → 尾盘保本 → 周五清仓 → 持有满1日
- 需连续 N 轮满足同一条件才执行（等价于前端 confirmSig 的 2 秒确认，防抖动）
- 每轮先 pull 最新云端快照再判定，已卖出的不会重复卖
- 三条尾盘锁利默认关闭，可在 guard_config.json 里打开

用法：
  python3 cloud_guard.py --once      # 跑一轮
  python3 cloud_guard.py --dry       # 只判定不写入
  python3 cloud_guard.py --loop      # 常驻，每 60 秒一轮
"""
import io, json, os, sys, time, datetime, urllib.request, base64

TOPIC = os.environ.get('CG_TOPIC', 'https://ntfy.sh/spv3acc2-u81058-x7k2m9q-5c8e1f3a7d9b2e6f')
# 2026-11-19 PATCH-08：默认改为 raw 实时源。原 Pages 静态站有 60~90s 重建延迟，
# 守卫在延迟窗口内读到的还是回滚前的旧账本(37笔)，会判定云端被回滚并反推 37 笔 → 死循环。
LEDGER_URL = os.environ.get('CG_LEDGER', 'https://raw.githubusercontent.com/hugoyeswood/stock-picker-v3/main/ledger.json')
OWNER = 'ds25jve'
SRC = 'cloudguard'

BASE = os.path.dirname(os.path.abspath(__file__))
_SYNC_HOLD = {'at': 0}
CFG_PATH = os.path.join(BASE, 'guard_config.json')
PEND_PATH = os.path.join(BASE, 'guard_pending.json')
STATE_PATH = os.path.join(BASE, 'guard_state.json')
LOG_PATH = os.path.join(BASE, 'cloud_guard.log')
DEC_PATH = os.path.join(BASE, 'guard_decisions.json')
DEC_PUSH_PATH = 'guard_decisions.json'   # 推到 V3 仓库根，供页面读取（一套规则）
GH_ENV = '/home/user/.doubao/github-creds/gh.env'
GH_REPO = 'hugoyeswood/stock-picker-v3'

DEFAULTS = {
    "enable": True,
    "rules": {
        "take_profit": True, "stop_loss": True, "friday_close": True, "hold_one_day": True,
    },
    "confirm_rounds": 2,
    "heartbeat_min": 10,
    "session": {"start_hm": 935, "end_hm": 1450},
}


def log(msg):
    line = '[%s] %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line)
    try:
        with io.open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def cfg():
    c = json.loads(json.dumps(DEFAULTS))
    try:
        raw = json.load(io.open(CFG_PATH, encoding='utf-8'))
        for k, v in raw.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    except Exception as e:
        log('config read fail, use defaults: %s' % e)
    return c


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={'User-Agent': 'cloudguard/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _body_n(body):
    try:
        n = len(json.loads(body['d'].get('myTrades') or '[]'))
        if body['d'].get('tc'):
            n = max(n, int(body['d']['tc']))
        return n
    except Exception:
        return 0


def _ledger_snapshot(now_ms):
    """GitHub ledger.json 兜底：ntfy 消息超 4KB 被静默丢弃时，守卫仍能读到权威账本。
    2026-11-26 修：raw.githubusercontent 走代理常 502，改为 raw -> Pages -> API 三路轮询，
    否则守卫读到的永远是旧账本、随后被 ROLLBACK 闸门拦住（10:08 就发生过一次）。"""
    urls = [LEDGER_URL,
            'https://hugoyeswood.github.io/stock-picker-v3/ledger.json',
            'https://api.github.com/repos/hugoyeswood/stock-picker-v3/contents/ledger.json?ref=main']
    for i, u in enumerate(urls):
        try:
            raw = http_get(u + ('&' if '?' in u else '?') + 'nc=' + str(int(time.time() * 1000)), 20)
            b = json.loads(raw) if i < 2 else json.loads(
                base64.b64decode(json.loads(raw).get('content') or '').decode('utf-8'))
            if isinstance(b, dict) and 'd' in b and b.get('at', 0) <= now_ms + 120000 and str(b.get('lock')) != '0':
                if i:
                    log('ledger source #%d 生效 %s' % (i, u.split('?')[0][-40:]))
                return b
        except Exception as e:
            log('ledger fallback fail #%d: %s' % (i, e))
    return None


def _pick_best(a, b, now_ms):
    if a is None:
        return b
    if b is None:
        return a
    # 2026-11-19 PATCH-08：结转快照(compact=1)优先。
    # 原规则「笔数最多者胜」在 PATCH-06 账本结转后失效：结转把 37 笔压到 10 笔，
    # 于是 ntfy 上的旧 37 笔快照永远压过新的 10 笔 → 守卫判定"云端被回滚"→ 反复反推 37 笔 → 死循环。
    def _c(x):
        v = x.get('compact')
        return 1 if (v is True or str(v) in ('1', 'true', 'True')) else 0
    ca, cb = _c(a), _c(b)
    if cb and not ca:
        return b
    if ca and not cb:
        return a
    if ca and cb:
        return b if b.get('at', 0) > a.get('at', 0) else a
    na, nb = _body_n(a), _body_n(b)
    if nb > na or (nb == na and b.get('at', 0) > a.get('at', 0)):
        return b
    return a


def pull_snapshot():
    """取云端最新的 master 快照，过滤未来时戳。
    窗口自适应：手机端长时间不通时 1h 窗口会空，逐级放大到 24h；
    ntfy 与 GitHub ledger.json 双源，按交易笔数（并列取最新时戳）择优。"""
    now_ms = time.time() * 1000
    best = None
    for win in ('1h', '6h', '12h', '24h'):
        try:
            raw = http_get(TOPIC + '/json?poll=1&since=' + win, 30).decode('utf-8', 'ignore')
        except Exception as e:
            log('pull fail (%s): %s' % (win, e))
            continue
        best = _pick_best(best, _pick_master(raw, now_ms), now_ms)
    lg = _ledger_snapshot(now_ms)
    if lg:
        log('ledger.json 候选 trades=%d' % _body_n(lg))
    best = _pick_best(best, lg, now_ms)
    if best:
        log('snapshot picked trades=%d budget=%.2f' % (_body_n(best), float(best['d'].get('myBudget') or 0)))
        return best
    return None


def _pick_master(raw, now_ms):
    best = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        if m.get('event') != 'message':
            continue
        try:
            body = json.loads(m.get('message', ''))
        except Exception:
            continue
        if not isinstance(body, dict) or 'd' not in body:
            continue
        at = body.get('at') or 0
        if at > now_ms + 120000:      # 未来时戳一律丢弃
            continue
        if str(body.get('lock')) == '0':   # 页面上报正常不带 lock 字段，仅从属设备显式置 0
            continue
        # 交易笔数优先（账本追加型，条数只会增）：防止「推得更晚但更旧」的设备把权威账本压掉
        try:
            n = len(json.loads(body['d'].get('myTrades') or '[]'))
            if body['d'].get('tc'):
                n = max(n, int(body['d']['tc']))
        except Exception:
            n = 0
        best_n = 0
        if best is not None:
            try:
                best_n = len(json.loads(best['d'].get('myTrades') or '[]'))
                if best['d'].get('tc'):
                    best_n = max(best_n, int(best['d']['tc']))
            except Exception:
                best_n = 0
        # 2026-11-19 PATCH-08b：结转快照(compact=1)优先，再比笔数。
        # 原逻辑在这里又按「笔数多」选了一遍（绕过 _pick_best），导致 compact 10 笔
        # 在 ntfy 源内输给旧 37 笔，守卫永远看不到结转结果。
        def _ck(x):
            v = (x or {}).get('compact')
            return 1 if (v is True or str(v) in ('1', 'true', 'True')) else 0
        c_new, c_old = _ck(body), _ck(best)
        if (best is None or c_new > c_old
                or (c_new == c_old and (n > best_n or (n == best_n and at > (best.get('at') or 0))))):
            body['_n'] = n
            best = body
    return best


def calc_fee(amount, kind):
    """与前端 calcFee 完全一致的费率：佣金 max(0.025%,5元) + 过户费 0.002% + 卖出印花税 0.1%"""
    comm = max(amount * 0.00025, 5.0)
    trans = amount * 0.00002
    stamp = amount * 0.001 if kind == 'sell' else 0.0
    return comm + trans + stamp


def calc_cash(d, trades):
    """按成交流水推算可用资金：本金 + 卖出回款 - 买入支出"""
    p0 = float(parse_val(d.get('myPrincipal'), 10000) or 10000)
    buy = sell = 0.0
    for t in trades or []:
        # 2026-11-19 PATCH-10：结转记录(adjust)直接把净现金贡献并入起点本金。
        # 缺这一行，守卫会把 compact 后的账本重算成 5370.22（页面 4891.11），
        # 触发 CASH DRIFT 后又把资金按错误流水纠正，两端继续对不上。
        if t.get('type') == 'adjust':
            try:
                p0 += float(t.get('delta') or 0)
            except Exception:
                pass
            continue
        try:
            q = float(t.get('qty') or 0)
            pr = float(t.get('price') or 0)
            amt = float(t.get('amount') or round(q * pr, 2))
        except Exception:
            continue
        # 2026-11-23：账本记录里的真实 fee 优先（券商实收），缺失才用模型估算；
        # 模型在 11-20 两笔卖出上多算 1.80，会让守卫把可用资金判成 9420.80 而非 9422.60
        try:
            _f = t.get('fee')
            _f = float(_f) if _f is not None and str(_f) != '' else None
        except Exception:
            _f = None
        if _f is not None and _f <= 0:
            _f = None
        if t.get('type') == 'sell' or (t.get('type') is None and t.get('reason')):
            sell += amt - (_f if _f is not None else calc_fee(amt, 'sell'))
        else:
            buy += amt + (_f if _f is not None else calc_fee(amt, 'buy'))
    return round(p0 + sell - buy, 2)


def reconcile_cash(d, trades, budget, auto_push=False):
    """闸门 C：可用资金必须等于流水推算值。
    budget 是各客户端独立写入的字段，重复卖出/旧快照回滚都会把它写漂，
    从而让总资产凭空虚增；这里一律以成交流水为准回写。"""
    try:
        cash = calc_cash(d, trades)
    except Exception as e:
        log('cash calc fail: %s' % e)
        return budget
    if abs(cash - float(budget or 0)) <= 1:
        return budget
    log('CASH DRIFT 账本%.2f -> 流水%.2f（差额%+.2f），按流水纠正'
        % (float(budget or 0), cash, cash - float(budget or 0)))
    if auto_push:
        d['myBudget'] = str(cash)
        try:
            push_snapshot(d, time.time() * 1000)
            log('cash corrected snapshot pushed')
        except Exception as e:
            log('cash correct push fail: %s' % e)
    return cash


def _pack_body(d, at_ms):
    return json.dumps({"v": 1, "at": int(at_ms), "src": SRC, "lock": 1, "owner": OWNER, "compact": 1, "d": d},
                      ensure_ascii=False, separators=(',', ':'))


# ntfy 单条消息硬上限 4096B，超了服务端仍回 200 但消息不入库（2026-11-18 已踩坑：
# 18 笔账本 4.5KB 被静默丢弃，导致回滚闸门永远读到旧的 15 笔账本）
MSG_LIMIT = 3900
_TRADE_KEYS = ('type', 'code', 'name', 'price', 'qty', 'amount', 'fee', 'time', 'auto', 'reason')


def norm_trades(trs):
    """账本规范化（2026-11-23 补丁）：type 缺失但 qty>0 且无卖出理由 → 视为 buy；缺 amount 补齐。
    防止历史脏记录（3 笔空 type 买入）导致“卖出透支”误判。"""
    out = []
    for t in (trs or []):
        if not isinstance(t, dict):
            continue
        if not t.get('type'):
            try:
                q = float(t.get('qty') or 0)
            except Exception:
                q = 0
            if q > 0 and not (t.get('reason') or t.get('sellReason')):
                t['type'] = 'buy'
        if t.get('type') in ('buy', 'sell') and t.get('amount') is None:
            try:
                t['amount'] = round(float(t['price']) * int(t['qty']), 2)
            except Exception:
                pass
        out.append(t)
    return out


def _compact_trade(t, level=1):
    """云端账本单笔瘦身：只保留资金重放必需字段。
    level=1 保留 name/amount；level=2 再丢掉 name 与 amount（金额可用 price*qty 还原，
    名称由前端按代码反查），用于账本接近 4KB 时避免消息被 ntfy 静默丢弃。"""
    tm = str(t.get('time') or '').replace('2026/', '')[:14]
    # PATCH-20261124：adjust 结转记录必须保留 delta。原先被精简成无 delta 的空壳，
    # 页面 replay 会把 11-16 结转(-462.80) 与 11-23 补记(-3.57) 都当 0，可用资金虚高 466.37。
    if t.get('type') == 'adjust':
        return {'type': 'adjust', 'delta': t.get('delta') or 0, 'time': tm}
    out = {'type': t.get('type'), 'code': t.get('code'), 'price': t.get('price'),
           'qty': t.get('qty'), 'fee': t.get('fee'), 'time': tm, 'auto': t.get('auto')}
    # 2026-11-24 修：瘦身时把 reason 丢了 → 页面交易记录里出现「无权据的自动卖出」
    # （14:40:49 依顿 6分清仓那笔即此）。卖出依据是用户「所有买卖靠数据分析」的硬要求，必须保留。
    _r = t.get('reason') or t.get('sellReason')
    if _r:
        out['reason'] = str(_r)[:40]
    if level == 1:
        out['name'] = t.get('name')
        out['amount'] = t.get('amount')
    return out


_DEC_LAST = {'sig': None, 'at': 0}


def _gh_token():
    t = os.environ.get('GH_TOKEN') or ''
    if t:
        return t
    try:
        for ln in io.open(GH_ENV, encoding='utf-8'):
            s2 = ln.strip()
            if s2.startswith('export '):
                s2 = s2[7:]
            if s2.startswith('GH_TOKEN'):
                return s2.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ''


def gh_put_content(path, text, msg):
    """PUT 一个小文件到 V3（Contents API）。失败只记日志，不影响守卫主流程。"""
    tok = _gh_token()
    if not tok:
        log('gh_put skip %s：无 GH_TOKEN' % path)
        return False
    url = 'https://api.github.com/repos/%s/contents/%s' % (GH_REPO, path)
    hdr = {'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json',
           'Content-Type': 'application/json', 'User-Agent': 'cloudguard/1.0'}
    sha = None
    try:
        req = urllib.request.Request(url + '?ref=main', headers=hdr)
        with urllib.request.urlopen(req, timeout=12) as r:
            sha = (json.loads(r.read().decode('utf-8')) or {}).get('sha')
    except Exception:
        pass
    body = {'message': msg, 'content': base64.b64encode(text.encode('utf-8')).decode('ascii')}
    if sha:
        body['sha'] = sha
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                     headers=hdr, method='PUT')
        with urllib.request.urlopen(req, timeout=15) as r:
            log('gh_put %s -> %s' % (path, r.status))
            return r.status in (200, 201)
    except Exception as e:
        log('gh_put fail %s: %s' % (path, e))
        return False


def push_decisions(dd):
    """把本轮决策（评分/结论）推 V3，页面据此展示并作为卖出同源依据。
    节流：距上次 <150 秒不推；内容无变化且 <600 秒不推。"""
    now = time.time()
    try:
        sig = json.dumps([(r['code'], r['act'], r['score']) for r in dd.get('hold', [])],
                         ensure_ascii=False, sort_keys=True)
    except Exception:
        sig = None
    if now - float(_DEC_LAST.get('at') or 0) < 150:
        return False
    if sig == _DEC_LAST.get('sig') and now - float(_DEC_LAST.get('at') or 0) < 600:
        return False
    ok = gh_put_content(DEC_PUSH_PATH, json.dumps(dd, ensure_ascii=False, indent=1),
                        'guard decisions %s' % dd.get('at', ''))
    if ok:
        _DEC_LAST['sig'] = sig
        _DEC_LAST['at'] = now
    return ok


def push_snapshot(d, at_ms):
    """写云端账本。ntfy 单条上限约 4KB，超限只做字段瘦身，绝不删交易记录
    （删记录会让前端按流水重算的可用资金/已实现盈亏全部失真）。"""
    body = _pack_body(d, at_ms)
    if len(body.encode('utf-8')) > MSG_LIMIT:
        slim = dict(d)
        logs = parse_val(d.get('myAutoLog'), [])
        if isinstance(logs, list) and len(logs) > 8:
            slim['myAutoLog'] = json.dumps(logs[:8], ensure_ascii=False)
        trs = norm_trades(parse_val(d.get('myTrades'), []))
        if isinstance(trs, list):
            slim['myTrades'] = json.dumps([_compact_trade(t) for t in trs], ensure_ascii=False)
        body = _pack_body(slim, at_ms)
        d = slim
        log('push 瘦身 -> %d B（交易 %d 笔全保留）'
            % (len(body.encode('utf-8')), len(trs) if isinstance(trs, list) else 0))
        if len(body.encode('utf-8')) > MSG_LIMIT and isinstance(trs, list):
            slim2 = dict(slim)
            slim2['myTrades'] = json.dumps([_compact_trade(t, 2) for t in trs], ensure_ascii=False)
            body = _pack_body(slim2, at_ms); d = slim2
            log('push 二级瘦身 -> %d B（丢 name/amount，名称按代码反查）' % len(body.encode('utf-8')))
        if len(body.encode('utf-8')) > MSG_LIMIT:
            log('WARN 账本仍超 %d B，需人工瘦身（周切换时清理）' % MSG_LIMIT)
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(TOPIC, data=body.encode('utf-8'),
                                         headers={'Content-Type': 'text/plain; charset=utf-8',
                                                  'User-Agent': 'cloudguard/1.0'},
                                         method='POST')
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status
        except Exception as e:
            last = e
            log('push attempt %d failed: %s' % (i + 1, e))
            time.sleep(2)
    raise last


def parse_val(v, default):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def get_quotes(codes):
    """腾讯行情批量；f[2]=纯代码 f[3]=现价 f[4]=昨收 f[32]=涨跌幅"""
    out = {}
    if not codes:
        return out
    q = ','.join(codes)
    try:
        raw = http_get('https://qt.gtimg.cn/q=' + q, 20).decode('gbk', 'ignore')
    except Exception as e:
        log('quote fail: %s' % e)
        return out
    for seg in raw.strip().split(';'):
        if '=' not in seg:
            continue
        f = seg.split('=')[1].strip('"').split('~')
        if len(f) < 35:
            continue
        try:
            # v_sh603088="1~宁波精达~603088~9.34~..." → f[0]=市场标识 f[1]=名称 f[2]=纯代码
            _o = {'name': f[1], 'now': float(f[3]), 'prev': float(f[4]), 'chg': f[32],
                  'high': float(f[33] or 0), 'low': float(f[34] or 0),
                  'amp': float(f[43] or 0), 'vr': float(f[49] or 0)}
            out[f[2][-6:]] = _o
        except Exception:
            continue
    return out


def build_holdings(trades):
    """与前端 autoHoldings 完全一致：只认 auto 记录，buy 加 qty/cost，sell 减 qty"""
    h = {}
    for t in trades:
        # PATCH-20261119-02 修持仓虚高：原实现 `if not (auto or type=='buy'): continue`
        # 会把「旧格式卖出记录」（type 缺失、仅靠 auto+reason 表示卖出）整条跳过，
        # 于是卖出不生效，历史仓位越滚越多 —— 守卫 09:39 干跑把飞乐/兴业/键邦/华微等
        # 早已清仓的票当成在手持仓并准备卖出（重复卖空风险）。改为完整方向判定。
        _sr = t.get('reason') or t.get('sellReason')
        is_buy0 = (t.get('type') == 'buy') or (t.get('type') is None and bool(t.get('auto')) and not _sr)
        is_sell0 = (t.get('type') == 'sell') or (t.get('type') is None and bool(t.get('auto')) and bool(_sr))
        if not (is_buy0 or is_sell0):
            continue
        code = t.get('code')
        if not code:
            continue
        if code not in h:
            h[code] = {'code': code, 'name': t.get('name', ''), 'qty': 0, 'cost': 0.0,
                       'tp': 0.0, 'sl': 0.0, 'entry': 0.0, 'buyDay': '', 'buyAt': 0}
        q = t.get('qty') or 0
        try:
            q = float(q)
        except Exception:
            q = 0
        # 兼容历史建仓记录：云端账本里自动买入可能只有 auto/day/qty/price，
        # 无 type 字段也无 amount。统一按 buy 处理，成本用 price*qty 兜底。
        is_buy = is_buy0
        if is_buy:
            h[code]['qty'] += q
            amt = float(t.get('amount') or 0)
            if amt <= 0:
                try:
                    amt = float(t.get('price') or 0) * q
                except Exception:
                    amt = 0.0
            h[code]['cost'] += amt
            if t.get('tp'):
                h[code]['tp'] = float(t['tp'])
            if t.get('sl'):
                h[code]['sl'] = float(t['sl'])
            if t.get('entry'):
                h[code]['entry'] = float(t['entry'])
            if t.get('day'):
                h[code]['buyDay'] = str(t['day'])
            if not h[code]['buyDay'] and t.get('time'):
                import re
                m = re.search(r'(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})', str(t['time']))
                if m:
                    h[code]['buyDay'] = '%s-%02d-%02d' % (m.group(1), int(m.group(2)), int(m.group(3)))
                else:
                    # 1118：兼容 '11/17 09:35:01' 这类无年份写法（年份按当前日推断）
                    m2 = re.search(r'(\d{1,2})[\/\-](\d{1,2})\D+\d{1,2}:\d{2}', str(t['time']))
                    if m2:
                        mo, dd = int(m2.group(1)), int(m2.group(2))
                        yr = datetime.datetime.now().year
                        if mo > datetime.datetime.now().month + 1:
                            yr -= 1
                        h[code]['buyDay'] = '%04d-%02d-%02d' % (yr, mo, dd)
            # 记录买入时刻（秒），供日内快进快出判断最短持有时间
            try:
                import re as _re
                mm = _re.search(r'(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})\D+(\d{1,2}):(\d{2})(?::(\d{2}))?',
                                str(t.get('time') or ''))
                if mm:
                    ts = datetime.datetime(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)),
                                          int(mm.group(4)), int(mm.group(5)),
                                          int(mm.group(6) or 0))
                    h[code]['buyAt'] = ts.timestamp()
                else:
                    m3 = _re.search(r'(\d{1,2})[\/\-](\d{1,2})\D+(\d{1,2}):(\d{2})(?::(\d{2}))?', str(t.get('time') or ''))
                    if m3:
                        mo, dd = int(m3.group(1)), int(m3.group(2))
                        yr = datetime.datetime.now().year
                        if mo > datetime.datetime.now().month + 1:
                            yr -= 1
                        ts = datetime.datetime(yr, mo, dd, int(m3.group(3)), int(m3.group(4)), int(m3.group(5) or 0))
                        h[code]['buyAt'] = ts.timestamp()
            except Exception:
                pass
        else:
            # 部分卖出：按当前均价等比例扣减成本，保证剩余持仓的每股成本不变
            sold = min(q, h[code]['qty'])
            if sold > 0 and h[code]['qty'] > 0:
                h[code]['cost'] -= (h[code]['cost'] / h[code]['qty']) * sold
            h[code]['qty'] -= q
            if h[code]['qty'] <= 0:
                h[code]['cost'] = 0.0
    # PATCH-20261119-13 止损线兜底（2026-11-19 13:20 加）
    # 根因：云端账本的 buy 记录普遍不带 tp/sl（建仓时前端没写进流水），于是
    # build_holdings 得到的 sl=0，止损判定 `h['sl'] > 0` 恒为假 —— 持仓全程裸奔、
    # 永不止损。飞乐音响亏超止损范围的真实原因在此，不是"操作慢"。
    # 兜底顺序：① 今日推荐名单的 stop_loss/take_profit → ② 成本均价×0.95 / ×1.10
    _filled = 0
    try:
        _rec = {}
        try:
            _j = json.load(io.open(os.path.join(BASE, 'short_term_rec.json'), encoding='utf-8'))
            for _s in (_j.get('stocks') or []):
                _c = str(_s.get('code') or '')
                if _c:
                    _rec[_c] = _s
        except Exception:
            pass
        for _c, _v in h.items():
            if _v['qty'] <= 0:
                continue
            _avg = (_v['cost'] / _v['qty']) if _v['qty'] else 0.0
            _r = _rec.get(_c) or {}
            if _v['sl'] <= 0:
                try:
                    _sl = float(_r.get('stop_loss') or 0)
                except Exception:
                    _sl = 0.0
                _v['sl'] = _sl if _sl > 0 else round(_avg * 0.95, 3)
                _filled += 1
            if _v['tp'] <= 0:
                try:
                    _tp = float(_r.get('take_profit') or 0)
                except Exception:
                    _tp = 0.0
                _v['tp'] = _tp if _tp > 0 else round(_avg * 1.10, 3)
            if _v['entry'] <= 0:
                try:
                    _e = float(_r.get('entry') or 0)
                except Exception:
                    _e = 0.0
                _v['entry'] = _e if _e > 0 else _avg
        if _filled:
            log('PATCH-13 止损兜底：%d 只补齐 sl/tp（账本未带，按推荐名单/成本推算）' % _filled)
    except Exception as _e:
        log('PATCH-13 兜底异常：%s' % _e)
    return [v for v in h.values() if v['qty'] > 0]


def load_state():
    try:
        s = json.load(io.open(STATE_PATH, encoding='utf-8'))
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def save_state(s):
    try:
        io.open(STATE_PATH, 'w', encoding='utf-8').write(json.dumps(s, ensure_ascii=False))
    except Exception as e:
        log('state write fail: %s' % e)


# ===== 预判下行主动卖出（2026-11-24 用户定稿：预测会跌就卖，不必等到亏损/止损线）=====
_KL_CACHE = {}
KL_PATH = os.path.join(BASE, 'kl_cache.json')


def _ma_from_tencent(code, n=30):
    """腾讯前复权日K兜底（东财 push2his 常被挡）。返回 ma5/ma10/ma20/last，失败给 {}。"""
    try:
        url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq'
               % (code, max(n, 25)))
        raw = http_get(url, 15).decode('utf-8', 'ignore')
        d = (json.loads(raw).get('data') or {}).get(code) or {}
        rows = d.get('qfqday') or d.get('day') or []
        cl = [float(r[2]) for r in rows if len(r) >= 6]
        if len(cl) < 10:
            return {}
        return {'ma5': sum(cl[-5:]) / len(cl[-5:]), 'ma10': sum(cl[-10:]) / len(cl[-10:]),
                'ma20': sum(cl[-20:]) / len(cl[-20:]), 'last': cl[-1]}
    except Exception:
        return {}


def get_kl_ma(code, n=21):
    """东财日K取 MA5/MA10/MA20（与选股脚本同源），5分钟文件级缓存，避免每轮重复抓"""
    global _KL_CACHE
    try:
        if not _KL_CACHE:
            _KL_CACHE = json.loads(io.open(KL_PATH, encoding='utf-8').read())
    except Exception:
        _KL_CACHE = {}
    hit = _KL_CACHE.get(code)
    if hit and time.time() - float(hit.get('t') or 0) < 300:
        return hit.get('ma') or {}
    secid = ('1.' if code.startswith('sh') else '0.') + code[-6:]
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3,f4&fields2=f51,f52,f53&klt=101&fqt=1&lmt=%d&end=20500101") % (secid, n)
    try:
        raw = http_get(url, 15).decode('utf-8', 'ignore')
        kl = json.loads(raw)['data']['klines']
        cl = [float(x.split(',')[2]) for x in kl]
        if not cl:
            return {}
        ma = {'ma5': sum(cl[-5:]) / len(cl[-5:]), 'ma10': sum(cl[-10:]) / len(cl[-10:]),
              'ma20': sum(cl[-20:]) / len(cl[-20:]), 'last': cl[-1]}
    except Exception as e:
        # 2026-11-24 修：东财接口偶断（Remote end closed）时原来直接 return {}，
        # 上层拿不到 MA5/MA10 → 预判下行评分为 0 → 双线已破也不卖（依顿 14:37 即此）。
        # 2026-11-26 补：东财彻底断时改走腾讯 K 线兜底（eastmoney 连续多轮失败会让守卫整体盲判）。
        _t = _ma_from_tencent(code, n)
        if _t:
            log('kline fallback tencent %s: ma5=%.2f ma10=%.2f（东财失败：%s）'
                % (code, _t.get('ma5') or 0, _t.get('ma10') or 0, e))
            _KL_CACHE[code] = {'t': time.time(), 'ma': _t}
            try:
                io.open(KL_PATH, 'w', encoding='utf-8').write(json.dumps(_KL_CACHE, ensure_ascii=False))
            except Exception:
                pass
            return _t
        # 改为回落到最近一次成功的 MA 缓存（3 天内有效，MA 变化慢，足够判定趋势）。
        _old = (hit or {}).get('ma') or {}
        if _old and time.time() - float((hit or {}).get('t') or 0) < 3 * 86400:
            log('kline fail %s: %s（回落缓存MA ma5=%.2f ma10=%.2f，缓存%d分钟前）'
                % (code, e, _old.get('ma5') or 0, _old.get('ma10') or 0,
                   int((time.time() - float(hit.get('t') or 0)) / 60)))
            return _old
        log('kline fail %s: %s（无可用缓存，本轮跳过技术判定）' % (code, e))
        return {}
    _KL_CACHE[code] = {'t': time.time(), 'ma': ma}
    try:
        io.open(KL_PATH, 'w', encoding='utf-8').write(json.dumps(_KL_CACHE, ensure_ascii=False))
    except Exception:
        pass
    return ma


def predict_down(h, q, pcfg, idx_pct):
    """技术面预判下行评分（2026-11-24 定稿，v2 降误杀版）。
    评分项(各1分)：①跌破MA5 ②跌破MA10 ③日内跌幅<=-1.5% ④距日内高点回落>=3%
    ⑤跑输大盘>=2个百分点 ⑥放量下跌(量比>=1.5)

    降误杀三道闸（用户 11-24「想避免卖飞代价」）：
      闸1 双线确认：必须同时跌破 MA5 与 MA10 才算趋势走坏（单线破不卖）；
      闸2 缩量豁免：量比<1.0 且日内跌幅>-2% 视为缩量回踩（健康回调），不卖；
      闸3 分批减仓：3 分只卖 50%，>=4 分才清仓，卖飞最多飞一半。
    返回 (score, det, ratio)，ratio=0 表示不卖。
    """
    win = pcfg.get('window') or [[945, 1125], [1310, 1445]]
    hm = now_hm()
    ok_win = False
    for w in win:
        try:
            if int(w[0]) <= hm <= int(w[1]):
                ok_win = True
                break
        except Exception:
            continue
    if not ok_win:
        return 0, [], 0.0
    price = float(q.get('now') or 0)
    prev = float(q.get('prev') or 0)
    high = float(q.get('high') or 0)
    if price <= 0 or prev <= 0:
        return 0, [], 0.0
    kl = get_kl_ma(h['code'])
    score = 0
    det = []
    ma5 = kl.get('ma5') or 0
    ma10 = kl.get('ma10') or 0
    below5 = bool(ma5 and price < ma5)
    below10 = bool(ma10 and price < ma10)
    chg = (price / prev - 1) * 100
    vr = float(q.get('vr') or 0)
    # 闸2：缩量回踩豁免（量没放大、跌得也不狠 → 看作正常回调，别卖飞）
    if bool(pcfg.get('shrink_exempt', True)) and vr > 0 and vr < 1.0 and chg > -2.0:
        return 0, ['缩量回踩豁免(量比%.2f)' % vr], 0.0
    if below5:
        score += 1; det.append('跌破MA5(%.2f)' % ma5)
    if below10:
        score += 1; det.append('跌破MA10(%.2f)' % ma10)
    if chg <= -1.5:
        score += 1; det.append('日内%.1f%%' % chg)
    if high > 0:
        drop = (high - price) / high * 100
        if drop >= 3:
            score += 1; det.append('距高点-%.1f%%' % drop)
    if idx_pct is not None and chg - idx_pct <= -2:
        score += 1; det.append('跑输大盘%.1f个点' % (idx_pct - chg))
    if vr >= 1.5 and chg < 0:
        score += 1; det.append('放量下跌(量比%.2f)' % vr)
    # 闸1：双线确认
    if bool(pcfg.get('need_double_ma', True)) and not (below5 and below10):
        return score, det, 0.0
    # 闸3：分批
    split = pcfg.get('split') or {'3': 0.5, '4': 1.0}
    min_score = int(pcfg.get('min_score', 3))
    if score < min_score:
        return score, det, 0.0
    ratio = float(split.get(str(score)) or split.get(str(min(score, score)) ) or 1.0)
    return score, det, ratio


def decide(h, price, hm, is_friday, day, rules, intra=None):
    """返回卖出原因；顺序与前端 autoTick 完全一致（关闭的规则跳过）"""
    cost_avg = (h['cost'] / h['qty']) if h['qty'] > 0 else 0
    # 日内快进快出：按成本价百分比止盈止损，覆盖双边费用后仍有净利才落袋
    intra = intra or {}
    if (rules.get('intraday') or intra.get('enable')) and cost_avg > 0:
        tp_pct = float(intra.get('tp_pct', 2.0)) / 100.0
        sl_pct = float(intra.get('sl_pct', 1.2)) / 100.0
        min_hold = int(intra.get('min_hold_min', 15)) * 60
        held = (time.time() - float(h.get('buyAt') or 0)) if h.get('buyAt') else 0
        if held >= min_hold:
            ret = price / cost_avg - 1
            if ret >= tp_pct:
                return '日内止盈%.2f%%' % (ret * 100)
            if ret <= -sl_pct:
                return '日内止损%.2f%%' % (ret * 100)
    if rules.get('take_profit') and h['tp'] > 0 and price >= h['tp']:
        return '止盈'
    if rules.get('stop_loss') and h['sl'] > 0 and price <= h['sl']:
        return '止损'
    if rules.get('friday_close') and is_friday and hm >= 1430:
        return '周五清仓（不过周末）'
    if rules.get('hold_one_day') and h['buyDay'] and h['buyDay'] < day and hm >= 940:
        return '持有满1日'
    return None


def calc_fee_sell(amount):
    """与前端一致的卖出费用：印花税 0.05% + 佣金 0.025%（最低5）+ 过户费 0.001%"""
    stamp = amount * 0.0005
    comm = max(amount * 0.00025, 5.0)
    transfer = amount * 0.00001
    return round(stamp + comm + transfer, 2)


def now_hm():
    n = datetime.datetime.now()
    return n.hour * 100 + n.minute


def in_session(s):
    n = datetime.datetime.now()
    if n.weekday() > 4:
        return False
    hm = now_hm()
    return s['start_hm'] <= hm < s['end_hm']



# ===== 页面停摆告警：交易时段内云端快照长时间不更新 → 手机页面可能已关闭 =====
STALL_MIN = 30


def push_alert(text, title='V3-STALL'):
    """推一条纯文本告警（不带 d 字段，不会被当成账本快照）"""
    try:
        req = urllib.request.Request(TOPIC, data=text.encode('utf-8'),
                                     headers={'Title': title, 'Tags': 'warning',
                                              'User-Agent': 'cloudguard/1.0'},
                                     method='POST')
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status
    except Exception as e:
        log('alert push failed: %s' % e)
        return None


def load_state():
    try:
        return json.load(open(STATE_PATH, encoding='utf-8'))
    except Exception:
        return {}


def save_state(st):
    try:
        json.dump(st, open(STATE_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    except Exception:
        pass


def check_stall(snap, dry=False):
    """返回 None=正常 / 'alerted'=已报警 / 'already_alerted'=今天已报过"""
    s = cfg().get('session', {})
    if not in_session(s):
        return None
    at = snap.get('at')
    if not at:
        return None
    gap_min = (time.time() * 1000 - int(at)) / 60000.0
    if gap_min < STALL_MIN:
        return None
    st = load_state()
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    if st.get('stall_alert_date') == today:
        return 'already_alerted'
    msg = ('[%s] V3 页面已 %d 分钟未上报心跳（最后 %s）。自动交易可能已停摆，'
           '请打开手机页面恢复每秒抓价与确认买卖。'
           % (datetime.datetime.now().strftime('%H:%M'), int(gap_min),
              datetime.datetime.fromtimestamp(int(at) / 1000).strftime('%H:%M:%S')))
    if dry:
        log('STALL (dry) ' + msg)
        return 'alerted'
    code = push_alert(msg)
    log('STALL ALERT -> status %s | %s' % (code, msg))
    st['stall_alert_date'] = today
    st['stall_alert_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    st['stall_alert_msg'] = msg
    save_state(st)
    return 'alerted'


def run_once(dry=False):
    c = cfg()
    if not c.get('enable', True):
        log('disabled by config')
        return {'skipped': 'disabled'}

    snap = pull_snapshot()
    if not snap:
        log('no cloud snapshot available')
        return {'skipped': 'no_snapshot'}

    d = snap.get('d', {})
    trades = norm_trades(parse_val(d.get('myTrades'), []))
    # 云端账本超 4KB 会裁掉最旧的卖出，用 tc 记录真实总笔数，避免回滚闸门误判
    total_cnt = int(parse_val(d.get('tc'), len(trades)) or len(trades))
    budget = float(parse_val(d.get('myBudget'), 0) or 0)
    auto_on = str(parse_val(d.get('autoTradeOn'), '1'))
    day = datetime.datetime.now().strftime('%Y-%m-%d')
    hm = now_hm()
    is_friday = datetime.datetime.now().weekday() == 4

    log('snapshot at=%s trades=%d budget=%.2f autoTradeOn=%s'
        % (datetime.datetime.fromtimestamp((snap.get('at') or 0) / 1000).strftime('%H:%M:%S'),
           len(trades), budget, auto_on))

    stall = check_stall(snap, dry)
    if stall:
        log('stall check -> %s' % stall)

    if auto_on == '0':
        log('autoTradeOn=0, skip')
        return {'skipped': 'auto_off'}

    holds = build_holdings(trades)
    if not holds:
        log('no auto holdings')
        return {'skipped': 'no_holdings'}

    # ===== 闸门 A：账本回滚保护 =====
    # 交易记录是追加型的，条数只会增不会减。若本轮读到的条数比本日已见过的少，
    # 说明有旧设备（手机 localStorage 落后）把旧快照推上来覆盖了权威账本。
    # 此时绝不能据其建仓/卖出，否则会把已卖出的仓位重复再卖一遍。
    state = load_state()
    counts = state.get('counts') or {}
    prev_cnt = int(counts.get(day) or 0)
    # 2026-11-19 PATCH-09：结转快照(compact=1)允许笔数变少。
    # PATCH-06 把 37 笔压成 10 笔后，这条防回滚规则会把合法结转当成"旧设备覆盖"拦下，
    # 并反推 37 笔，形成死循环。带 compact 标记才放行，其余场景防护不变。
    _snap_c = 1 if (str((snap or {}).get('compact')) in ('1', 'true', 'True')
                    or (snap or {}).get('compact') is True) else 0
    if total_cnt < prev_cnt and not _snap_c:
        log('LEDGER ROLLBACK 拦截：本轮 trades=%d < 已见 %d，判定旧设备覆盖，本轮零动作'
            % (len(trades), prev_cnt))
        # 自愈：把最近一次权威账本推回云端，压掉旧设备的覆盖
        try:
            good = (state.get('lastGood') or {}).get('d')
            good_tr = parse_val(good.get('myTrades'), []) if good else []
            if good and len(good_tr) >= prev_cnt:
                code = push_snapshot(good, time.time() * 1000)
                log('ROLLBACK 自愈：已回推权威账本 status=%s trades=%d' % (code, len(good_tr)))
            elif good:
                log('ROLLBACK 自愈跳过：lastGood 仅 %d 笔 < 已见 %d，拒绝用旧账本覆盖'
                    % (len(good_tr), prev_cnt))
        except Exception as e:
            log('ROLLBACK 自愈失败: %s' % e)
        return {'skipped': 'rollback', 'trades': len(trades), 'seen': prev_cnt}
    counts[day] = total_cnt
    state['counts'] = counts
    # PATCH-16：lastGood 必须携带 compact/src/lock/owner，否则下一次比对时
    # 该快照会被 GitHub 上的旧非 compact 快照按「笔数优先」压掉（11-20 卖出丢失同源）
    state['lastGood'] = {'at': time.time(), 'd': d, 'compact': 1, 'src': SRC, 'lock': 1, 'owner': OWNER}
    save_state(state)

    # ===== 闸门 B：当日已卖出的代码不再重复卖 =====
    # 闸门 B：按「批次数」限制 —— 当日该票卖出的笔数不得多于买入的笔数。
    # 这样既拦住「同一仓位被重复卖出」（买1笔只能卖1笔），
    # 又允许分批建仓的票逐批了结（买2笔可卖2笔）。
    lots_today = {}
    solds_today = {}
    for t in trades:
        code = t.get('code')
        if not code:
            continue
        is_buy = (t.get('type') == 'buy') or (t.get('type') is None and t.get('auto') and not t.get('reason'))
        d0 = str(t.get('day') or '')
        if not d0 and t.get('time'):
            import re as _r
            m0 = _r.search(r'(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})', str(t['time']))
            if m0:
                d0 = '%s-%02d-%02d' % (m0.group(1), int(m0.group(2)), int(m0.group(3)))
        if d0 != day:
            continue
        if is_buy:
            lots_today[code] = lots_today.get(code, 0) + 1
        else:
            solds_today[code] = solds_today.get(code, 0) + 1

    quotes = get_quotes([h['code'] for h in holds])
    if not quotes:
        log('no quotes')
        return {'skipped': 'no_quotes'}
    # 大盘涨跌幅（用于「跑输大盘」判定）
    idx_pct = None
    try:
        _iq = get_quotes(['sh000001']).get('000001') or {}
        if _iq.get('chg'):
            idx_pct = float(_iq['chg'])
    except Exception:
        idx_pct = None

    # 读 pending（连续确认）
    try:
        pend = json.load(io.open(PEND_PATH, encoding='utf-8'))
    except Exception:
        pend = {}

    actions = []
    dec_rows = []
    new_pend = {}
    for h in holds:
        q = quotes.get(h['code'][-6:])
        if not q:
            continue
        price = q['now']
        cost_avg = (h['cost'] / h['qty']) if h['qty'] > 0 else 0
        _rt = 0.0
        _sc = 0
        _det = []
        reason = decide(h, price, hm, is_friday, day, c.get('rules', {}), c.get('intraday') or {})
        # 2026-11-24：无硬规则触发时，再跑一次「预判下行」技术卖出（浮亏/浮盈都会卖）
        pcfg = c.get('predict') or {}
        if not reason and pcfg.get('enable'):
            try:
                _sc, _det, _rt = predict_down(h, q, pcfg, idx_pct)
                if _rt > 0:
                    reason = '预判下行(%d分:%s)' % (_sc, '、'.join(_det[:3]))
                    log('  PREDICT %s 评分=%d 减仓比例=%.0f%% %s'
                        % (h['code'], _sc, _rt * 100, '/'.join(_det)))
            except Exception as e:
                log('  predict fail %s: %s' % (h['code'], e))
        pct = (price / cost_avg - 1) * 100 if cost_avg else 0
        intra = c.get('intraday') or {}
        note = ''
        if intra.get('enable') and cost_avg > 0:
            tp_p = float(intra.get('tp_pct', 2.0))
            sl_p = float(intra.get('sl_pct', 1.2))
            need = (cost_avg * (1 + tp_p / 100.0) / price - 1) * 100 if price else 0
            note = ' | 日内 止盈差%+.2f%% 止损距%+.2f%%' % (need, (cost_avg * (1 - sl_p / 100.0) / price - 1) * 100)
        log('  %s %s qty=%.0f 成本%.3f 现价%.2f (%+.2f%%) tp=%.2f sl=%.2f -> %s%s'
            % (h['code'], h['name'], h['qty'], cost_avg, price, pct, h['tp'], h['sl'],
               reason or '持有', note))
        dec_rows.append({'code': h['code'], 'name': h['name'], 'px': price,
                         'tp': h['tp'], 'sl': h['sl'],
                         'cost': (round(cost_avg, 3) if cost_avg else 0),
                         'qty': h['qty'], 'score': int(_sc), 'det': list(_det)[:3],
                         'act': 'sell' if reason else 'hold', 'reason': reason or ''})
        if not reason:
            continue
        if solds_today.get(h['code'], 0) >= max(1, lots_today.get(h['code'], 1)):
            log('  SKIP %s：本日已卖出 %d 笔 >= 买入 %d 笔，防重复卖出'
                % (h['code'], solds_today.get(h['code'], 0), lots_today.get(h['code'], 1)))
            continue
        key = h['code']
        prev = pend.get(key) or {}
        cnt = int(prev.get('cnt') or 0) + 1 if prev.get('reason') == reason else 1
        need = int(c.get('confirm_rounds', 2))
        # PATCH-20261119-01 止损一轮即走：止损/日内止损不等第二轮确认
        # 根因：guard_daemon 08:34 后死亡 + session 935 才开始 + confirm_rounds=2 再等一轮，
        #       三层延迟叠加，飞乐音响 09:31 已跌破 4.6265，人工到 09:31 才卖在 4.63，
        #       若守卫正常应更早离场。止损属风控，宁可早走不可拖。
        if reason and ('止损' in str(reason)):
            need = 1
        if cnt >= need:
            _ratio = 1.0
            if reason.startswith('预判下行('):
                try:
                    _ratio = float(_rt or 1.0)
                except Exception:
                    _ratio = 1.0
            _q = float(h['qty'])
            if _ratio < 1.0:
                _q = float(int(_q * _ratio / 100) * 100)   # 整手向下
                if _q <= 0:
                    _q = min(100.0, float(h['qty']))
            actions.append({'h': h, 'price': price, 'reason': reason, 'qty': _q})
        else:
            new_pend[key] = {'reason': reason, 'cnt': cnt, 'at': time.time()}

    # 未触发的一律清空 pending
    for k in list(pend.keys()):
        if k not in new_pend and k not in [a['h']['code'] for a in actions]:
            pass
    try:
        io.open(PEND_PATH, 'w', encoding='utf-8').write(json.dumps(new_pend, ensure_ascii=False))
    except Exception as e:
        log('pending write fail: %s' % e)

    # 决策快照落盘 + 推 V3（页面同源展示/卖出依据）
    try:
        _dd = {'at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'date': day,
               'idx': (round(idx_pct, 2) if isinstance(idx_pct, (int, float)) else None),
               'session': [hm, c.get('session') or {}], 'hold': dec_rows}
        io.open(DEC_PATH, 'w', encoding='utf-8').write(json.dumps(_dd, ensure_ascii=False, indent=1))
        push_decisions(_dd)
    except Exception as e:
        log('decisions fail: %s' % e)

    # n27 持仓快照推 V3：页面以 holdings.json 为两端权威（手机/电脑同一份持仓）
    try:
        if time.time() - _SYNC_HOLD.get('at', 0) > 300:
            _SYNC_HOLD['at'] = time.time()
            _r = __import__('subprocess').run([sys.executable, os.path.join(BASE, 'sync_holdings_v3.py')],
                                              capture_output=True, text=True, timeout=150)
            _line = ((_r.stdout or _r.stderr).strip().splitlines() or [''])[0]
            log('sync_holdings: ' + _line)
    except Exception as e:
        log('sync_holdings fail: %s' % e)

    if not actions:
        log('no action this round')
        # 即使不动作也要对账：账本可用资金漂了就地按流水纠正
        reconcile_cash(d, trades, budget, auto_push=True)
        return {'acted': 0}

    if dry:
        for a in actions:
            log('DRY-RUN would sell %s %s qty=%.0f @%.2f reason=%s'
                % (a['h']['code'], a['h']['name'], a['h']['qty'], a['price'], a['reason']))
        return {'acted': 0, 'dry': len(actions)}

    # 二次校验：push 前重新 pull 一次，避免与手机端同一秒判定造成双卖
    logs_pre = None
    snap2 = pull_snapshot()
    if snap2:
        d2 = snap2.get('d', {})
        tr2 = parse_val(d2.get('myTrades'), [])
        holds2 = {}
        for h2 in build_holdings(tr2):
            holds2[h2['code']] = h2['qty']
        kept = []
        for a in actions:
            if holds2.get(a['h']['code'], 0) >= a['qty']:
                kept.append(a)
            else:
                log('SKIP %s：持仓已被手机端处理（需 %s -> 实际 %s）'
                    % (a['h']['code'], a['qty'], holds2.get(a['h']['code'])))
        actions = kept
        trades = tr2
        budget = float(parse_val(d2.get('myBudget'), budget) or budget)
        d = d2
        logs0 = parse_val(d.get('myAutoLog'), [])
        if isinstance(logs0, list):
            logs_pre = logs0
        else:
            logs_pre = []

    if not actions:
        log('all actions cancelled after re-check')
        return {'acted': 0, 'recheck_cancel': True}

    # 执行卖出：构造新 trades + budget + 日志
    new_trades = list(trades)
    logs = logs_pre if isinstance(logs_pre, list) else parse_val(d.get('myAutoLog'), [])
    if not isinstance(logs, list):
        logs = []
    ts_txt = datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    for a in actions:
        h, price, reason = a['h'], a['price'], a['reason']
        qty = float(a.get('qty') or h['qty'])
        amount = round(price * qty, 2)
        fee = calc_fee_sell(amount)
        new_trades.append({'type': 'sell', 'code': h['code'], 'name': h['name'], 'price': price,
                           'qty': qty, 'amount': amount, 'fee': fee, 'time': ts_txt,
                           'auto': True, 'reason': reason, 'src': SRC})
        budget += amount - fee
        pnl = amount - (h['cost'] / h['qty'] * qty if h['qty'] else 0) - fee
        logs.insert(0, '[%s] 🤖 云端卖出 · %s %.0f股 @¥%.2f（%s）回款¥%.2f｜本笔盈亏约%+.0f元'
                    % (datetime.datetime.now().strftime('%H:%M'), h['name'], qty, price, reason,
                       amount - fee, pnl))
        log('SOLD %s %s qty=%.0f @%.2f reason=%s pnl=%+.2f'
            % (h['code'], h['name'], qty, price, reason, pnl))

    d['myTrades'] = json.dumps(new_trades, ensure_ascii=False)
    # 写盘前统一按流水校准可用资金，避免客户端预算漂移被继承下来
    budget = reconcile_cash(d, new_trades, budget)
    d['myBudget'] = str(round(budget, 2))
    d['myAutoLog'] = json.dumps(logs[:30], ensure_ascii=False)

    # 记录本日已卖出代码，防止旧快照回滚后被重复卖出
    try:
        st = load_state()
        sold = st.get('sold') or {}
        lst = list(sold.get(day) or [])
        for a in actions:
            if a['h']['code'] not in lst:
                lst.append(a['h']['code'])
        sold[day] = lst
        st['sold'] = sold
        cnts = st.get('counts') or {}
        cnts[day] = max(int(cnts.get(day) or 0), int(parse_val(d.get('tc'), 0) or 0) + len(actions))
        st['counts'] = cnts
        save_state(st)
    except Exception as e:
        log('sold-record fail: %s' % e)
    try:
        code = push_snapshot(d, time.time() * 1000)
        log('pushed status=%s' % code)
        # 卖出成功后必须把「成交后的权威账本」记为 lastGood，
        # 否则下一轮回滚闸门会拿成交前的旧账本把本次卖出覆盖掉（2026-11-18 实盘已踩坑）
        try:
            st2 = load_state()
            st2['lastGood'] = {'at': time.time(), 'd': d, 'compact': 1, 'src': SRC, 'lock': 1, 'owner': OWNER}
            cnts2 = st2.get('counts') or {}
            cnts2[day] = max(int(cnts2.get(day) or 0), len(new_trades))
            st2['counts'] = cnts2
            save_state(st2)
        except Exception as e2:
            log('lastGood update fail: %s' % e2)
    except Exception as e:
        log('push fail: %s' % e)
        # 推送失败：把本轮动作写回 pending，下一轮直接重试卖出，不会因为丢了 push 就漏卖
        try:
            need = int(c.get('confirm_rounds', 2))
            retry_pend = dict(new_pend)
            for a in actions:
                retry_pend[a['h']['code']] = {'reason': a['reason'], 'cnt': need, 'at': time.time()}
            io.open(PEND_PATH, 'w', encoding='utf-8').write(json.dumps(retry_pend, ensure_ascii=False))
            log('pending restored for retry: %s' % list(retry_pend.keys()))
        except Exception as e2:
            log('pending restore fail: %s' % e2)
        return {'acted': 0, 'push_fail': str(e)}
    return {'acted': len(actions)}



REPUB_PATH = os.path.join(BASE, 'guard_republish.json')


def republish(dry=False, min_gap_min=5):
    """把最近一次权威账本按固定周期重推到 ntfy。

    原因：ntfy 免费通道只保留有限时长的消息，而守卫收盘后不再推送；手机端隔夜/周末
    恢复时拉不到权威快照，就会拿本地旧账本顶替（可用 4568.89 这类错误即由此产生）。
    这里不改账本内容，只保证"云端始终有一份可读的权威快照"，且逐轮限频。
    """
    st = load_state()
    last = st.get('lastGood') or {}
    dd = last.get('d')
    if not dd:
        return {'skipped': 'no_lastgood'}
    try:
        rr = json.loads(io.open(REPUB_PATH, encoding='utf-8').read())
    except Exception:
        rr = {}
    gap = (time.time() - float(rr.get('at') or 0)) / 60.0
    trades = len(parse_val(dd.get('myTrades'), []))
    if gap < min_gap_min:
        return {'skipped': 'fresh', 'gap_min': round(gap, 1), 'trades': trades}
    if dry:
        return {'dry': True, 'trades': trades}
    code = push_snapshot(dd, int(time.time() * 1000))
    io.open(REPUB_PATH, 'w', encoding='utf-8').write(
        json.dumps({'at': int(time.time()), 'code': code, 'trades': trades}, ensure_ascii=False))
    log('REPUBLISH lastGood status=%s trades=%d' % (code, trades))
    return {'republished': code, 'trades': trades}


def main():
    args = sys.argv[1:]
    dry = '--dry' in args
    force = '--force' in args
    if '--republish' in args:
        try:
            r = republish(dry)
            log('republish result: %s' % json.dumps(r, ensure_ascii=False))
        except Exception as e:
            log('republish fail: %s' % e)
        return
    if '--loop' in args:
        log('loop mode start')
        while True:
            try:
                c = cfg()
                if in_session(c.get('session', {'start_hm': 935, 'end_hm': 1450})):
                    run_once(dry)
                else:
                    log('out of session, idle')
            except Exception as e:
                log('round error: %s' % e)
            time.sleep(60)
    else:
        # --once 也守时段，避免竞价前用昨收价误判；--force 用于人工验证
        c = cfg()
        if not force and not in_session(c.get('session', {'start_hm': 935, 'end_hm': 1450})):
            log('out of session, skip (use --force to override)')
            return
        r = run_once(dry)
        log('result: %s' % json.dumps(r, ensure_ascii=False))


if __name__ == '__main__':
    main()
