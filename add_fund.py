# -*- coding: utf-8 -*-
"""
【资金转入端口】add_fund.py
====================================================
往资金账户里"转入"一笔钱，系统就会按新的可用资金来筛股和提示买卖点。

怎么用（简单）：
  1. 运行：  python add_fund.py
  2. 按提示输入转入金额（元）和备注
  3. 程序自动记入 wallet.json（资金账户）
  4. 重新跑 build_site.py，页面上的"当前可用资金"和"能买几手"就会更新

注意：这里的资金是你给自己定的"预算/本金"数字，用于选股范围。
真实转账、真实买卖永远只在你自己的券商官方 App 里操作。
====================================================
"""
import json, os
from datetime import datetime

WALLET = "wallet.json"

def load():
    if os.path.exists(WALLET):
        with open(WALLET, encoding="utf-8") as f:
            return json.load(f)
    return {"available": 0.0, "currency": "CNY", "ledger": []}

def save(w):
    with open(WALLET, "w", encoding="utf-8") as f:
        json.dump(w, f, ensure_ascii=False, indent=2)

def main():
    w = load()
    print("=" * 30)
    print("  资金转入端口")
    print("=" * 30)
    print(f"当前可用资金: ¥{w['available']:.2f}")
    print(f"历史转入记录: {len(w['ledger'])} 笔")
    print("（直接回车 = 结束退出）")
    while True:
        s = input("\n输入本次转入金额（元）: ").strip()
        if not s:
            print("已保存，退出。")
            break
        try:
            amt = float(s)
            if amt <= 0:
                print("金额必须大于 0，请重输。")
                continue
        except ValueError:
            print("请输入数字，例如 2000。")
            continue
        note = input("备注（可跳过，直接回车）: ").strip()
        w["ledger"].append({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "amount": amt,
            "note": note,
        })
        w["available"] = round(w["available"] + amt, 2)
        save(w)
        print(f"✓ 已转入 ¥{amt:.2f}，当前可用资金 ¥{w['available']:.2f}")
        # 继续循环，可连续转多笔

if __name__ == "__main__":
    main()
