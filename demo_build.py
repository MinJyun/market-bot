#!/usr/bin/env python3
"""產生 demo 分頁的靜態資料:某分點「當沖打最兇」的幾檔個股 × 日內走勢 × 逐價位。

為什麼是「當沖」而不是「買超/淨額」:這個分點有大戶在打大量當沖單,買賣幾乎
相抵,用淨額排序反而會把他排到最底下(2026-09-11 台達電:元大進出 1,572 張,
淨額只有 +3 張)。所以篩選用三個條件:

    當沖量 dt   = min(買股數, 賣股數)      同日買賣相抵的部分
    當沖金額    = dt × (買均價+賣均價)/2   → 濾掉小型股的雜訊(使用者要的成交額條件)
    當沖佔量    = dt ÷ 日線成交量          → 這個分點在該股當沖裡的份量
    對稱度      = |買−賣| ÷ dt             → 越小越純粹是沖,不是邊沖邊建倉
    振幅        = (最高−最低) ÷ 昨收        → 沒有振幅就沒有利潤空間

**振幅門檻的理由(實測 2026-09-11 元大,不是猜的)**:把「相抵損益 ÷ 相抵金額」
當成這筆當沖的報酬率,它與振幅明顯同向 ——

    3624 光頡  振幅 15.42%   8.68 億 → −731 萬   0.84%
    6173 信昌電 振幅 13.56%  16.86 億 → +1,962 萬 1.16%
    3026 禾伸堂 振幅  8.22%   9.90 億 → +1,041 萬 1.05%
    2303 聯電  振幅  2.81%  14.68 億 → −414 萬   0.28%
    2308 台達電 振幅  2.39%  25.31 億 → −116 萬   0.05%
    2357 華碩  振幅  1.82%   6.41 億 → −24 萬    0.04%

台達電進出 25 億只吃到 0.05%,信昌電 16.86 億吃到 1.16%,差 20 倍。相抵量最大
的那檔不見得是有戲的那檔,所以振幅要當門檻而不只是欄位。

昨收用 `close − spread` 算(price 表的 spread 是當日漲跌點數),不另外查前一
交易日 —— 少一次 join,而且遇到停牌、首日上市這些情況不會拿到錯的前一日。

**分母一律用 price 表的日線成交量**,不是分點合計 —— 分點成交量系統性少於
官方成交量(鉅額交易等未歸屬到分點,實測單日可差 41%)。

**dt 是「這個分點同日買賣相抵的量」,不等於證交所定義的當沖**:分點資料看不出
是同一個客戶一買一賣,也可能是兩個客戶各做一邊。這是分點資料能逼近的極限,
UI 上要照這個講法標示,不要寫成「當沖」二字了事。

輸出(都在 web/demo/,已 gitignore —— 含 FinMind 授權的逐筆成交與逐價位,
本 repo 是 public):
    index.json     篩選結果清單 + 每檔的指標
    <股票代號>.json 該股的 quote / 逐筆成交 / 該分點逐價位 / 全市場逐價位

用法:
    python3 demo_build.py                       # 預設 9800 × 最新交易日
    python3 demo_build.py --date 2026-09-11 --bno 9800 --limit 10
"""
import argparse
import json
import pathlib
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DB = HERE / "data" / "broker.db"
OUT = HERE / "web" / "demo"
ENV = HERE / ".env"
API = "https://api.finmindtrade.com/api/v4"


def get_token():
    """與 finmind_backfill.py 同一套:環境變數優先,其次 .env。"""
    import os
    tok = os.environ.get("FINMIND_TOKEN", "").strip()
    if not tok and ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "FINMIND_TOKEN":
                tok = v.strip().strip('"').strip("'")
                break
    if not tok:
        sys.exit("缺少 FINMIND_TOKEN(見 .env.example)")
    return tok


def api(token, dataset, stock_id, d, tries=4):
    """FinMind 單日查詢。

    **start_date 與 end_date 一律成對送**:只給 start_date 時回應會夾帶之後的
    日期(finmind_etf.py 踩過,當時誤判成 FinMind 的日期偏移)。
    """
    q = urllib.parse.urlencode({"dataset": dataset, "data_id": stock_id,
                                "start_date": d, "end_date": d, "token": token})
    for i in range(tries):
        try:
            r = urllib.request.urlopen(f"{API}/data?{q}", timeout=180)
            j = json.loads(r.read())
            if j.get("status") != 200:
                sys.exit(f"API 錯誤 {j.get('status')}: {j.get('msg')}")
            return j.get("data") or []
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("msg", "")
            except Exception:
                msg = ""
            if e.code != 429 and e.code < 500:      # 4xx 是權限/參數,重試無意義
                sys.exit(f"HTTP {e.code} — {msg}")
            if i == tries - 1:
                sys.exit(f"HTTP {e.code} 重試 {tries} 次仍失敗")
        except Exception as e:
            if i == tries - 1:
                sys.exit(f"重試 {tries} 次仍失敗:{e}")
        time.sleep(2 ** i + 1)


def stock_names():
    """合併 data/ 下所有股票池檔的代號→名稱(靠欄位判斷,不靠檔名)。"""
    out = {}
    for f in sorted((HERE / "data").glob("*.json")):
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("stock_id"):
                    out[str(r["stock_id"])] = r.get("name", "")
    return out


def screen(conn, date8, bno, min_amt, min_pct, max_asym, min_range, limit):
    """挑出該分點當日「相抵量」最可觀的幾檔。回傳 dict 清單。"""
    rows = conn.execute("""
        SELECT b.stock_id,
               MIN(b.buy_sh, b.sell_sh)                       AS dt_sh,
               b.buy_sh, b.sell_sh, b.buy_vwap, b.sell_vwap,
               p.volume, p.amount, p.open, p.high, p.low, p.close, p.spread
        FROM broker_daily b
        JOIN price p ON p.data_date = b.data_date AND p.stock_id = b.stock_id
        WHERE b.data_date = ? AND b.bno = ? AND p.volume > 0
    """, (date8, bno)).fetchall()

    out = []
    for (sid, dt_sh, buy, sell, bv, sv, vol, amt,
         op, hi, lo, cl, sp) in rows:
        if not dt_sh:
            continue
        mid = ((bv or 0) + (sv or 0)) / 2
        dt_amt = dt_sh * mid
        dt_pct = dt_sh * 100.0 / vol
        asym = abs(buy - sell) * 100.0 / dt_sh
        prev = cl - sp                      # 昨收 = 今收 − 當日漲跌點數
        rng = (hi - lo) * 100.0 / prev if prev else 0
        if (dt_amt < min_amt or dt_pct < min_pct or asym >= max_asym
                or rng < min_range):
            continue
        dt_pnl = round((sv - bv) * dt_sh) if bv and sv else 0
        out.append({
            "stock_id": sid,
            "dt_sh": dt_sh, "dt_amount": round(dt_amt),
            "dt_pct": round(dt_pct, 2), "asym_pct": round(asym, 1),
            "prev_close": round(prev, 2), "range_pct": round(rng, 2),
            "chg_pct": round(sp * 100.0 / prev, 2) if prev else 0,
            # 相抵報酬率:這筆當沖賺賠佔投入金額多少。振幅門檻的理由就在這欄。
            "dt_roi": round(dt_pnl * 100.0 / dt_amt, 2) if dt_amt else 0,
            "buy_sh": buy, "sell_sh": sell,
            "buy_vwap": bv, "sell_vwap": sv,
            "net_sh": buy - sell,
            # 相抵部分的已實現損益 =(賣均價−買均價)× 相抵股數。就是
            # web/server.py trade_pnl() 的已實現項;單日內不跨除權,不必過 adj_daily。
            "dt_pnl": dt_pnl,
            "part_pct": round((buy + sell) * 50.0 / vol, 2),
            "volume": vol, "amount": amt,
            "open": op, "high": hi, "low": lo, "close": cl, "spread": sp,
        })
    out.sort(key=lambda r: -r["dt_amount"])
    return out[:limit]


def build_stock(token, sid, date, bno):
    """抓該股該日的逐筆成交與逐價位,整理成前端要的形狀。"""
    ticks = api(token, "TaiwanStockPriceTick", sid, date)
    rows = api(token, "TaiwanStockTradingDailyReport", sid, date)

    # ⚠ 別拿「每個價位 Σ買 == Σ賣」當驗證條件。撮合的本質是相等,但實測 10 檔
    # 有 2 檔各有一個價位對不上(聯電 138.0 差 50 股、台達電 1605 差 4,000 股),
    # 已回頭打原始 API 確認是來源資料本身如此,不是聚合寫錯。要驗就給容差。
    #
    # ⚠ TaiwanStockTradingDailyReport **含零股**,逐筆成交與日線**不含**。
    # 實測 2026-09-11 台達電:逐價位在 1640 元有 857 股(元大賣 118 股),而當日
    # 最高價是 1635、逐筆成交最高也是 1635 —— 857 與 118 都不是整張,是零股。
    # 後果是逐價位的價格範圍可能超出當日高低,價格軸要取兩者聯集才不會裁掉。
    def secs(t):
        h, m, s = t.split(":")
        return round(int(h) * 3600 + int(m) * 60 + float(s), 3)

    # 盤後定價交易(13:30 之後)另外收:混進日內走勢會讓 x 軸多出一段空白。
    intraday, after = [], []
    for r in ticks:
        t = secs(r["Time"])
        # TickType:2=外盤(主動買)、1=內盤(主動賣)、0=無法判定
        item = [t, float(r["deal_price"]), int(r["volume"]), int(r["TickType"])]
        (after if t > 13.5 * 3600 else intraday).append(item)
    intraday.sort(key=lambda x: x[0])
    after.sort(key=lambda x: x[0])

    mine, market = {}, {}
    for r in rows:
        p = float(r["price"])
        b, s = int(r["buy"] or 0), int(r["sell"] or 0)
        m = market.setdefault(p, [0, 0])
        m[0] += b; m[1] += s
        if str(r["securities_trader_id"]) == bno:
            k = mine.setdefault(p, [0, 0])
            k[0] += b; k[1] += s
    pack = lambda d: [{"p": p, "b": v[0], "s": v[1]}
                      for p, v in sorted(d.items(), reverse=True)]
    return pack(mine), pack(market), intraday, after, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYY-MM-DD(預設 DB 最新有資料日)")
    ap.add_argument("--bno", default="9800", help="分點代號")
    ap.add_argument("--limit", type=int, default=10, help="最多取幾檔")
    ap.add_argument("--min-amount", type=float, default=5.0,
                    help="相抵金額門檻(億元)")
    ap.add_argument("--min-pct", type=float, default=8.0,
                    help="相抵量佔日線成交量的門檻(%%)")
    ap.add_argument("--max-asym", type=float, default=20.0,
                    help="對稱度上限(%%);越小越純粹是沖")
    ap.add_argument("--min-range", type=float, default=3.0,
                    help="振幅下限(%%);沒有振幅就沒有利潤空間")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    date8 = (args.date.replace("-", "") if args.date else
             conn.execute("SELECT MAX(data_date) FROM fetched"
                          " WHERE rows > 0").fetchone()[0])
    date = f"{str(date8)[:4]}-{str(date8)[4:6]}-{str(date8)[6:]}"

    picks = screen(conn, int(date8), args.bno, args.min_amount * 1e8,
                   args.min_pct, args.max_asym, args.min_range, args.limit)
    if not picks:
        sys.exit(f"{date} 分點 {args.bno} 沒有符合條件的個股,放寬門檻再試")

    names = stock_names()
    broker = json.loads((HERE / "data" / "broker_names.json").read_text(
        encoding="utf-8")) if (HERE / "data" / "broker_names.json").exists() else {}
    bname = broker.get(args.bno, args.bno)

    print(f"{date} 分點 {args.bno}({bname}):符合條件 {len(picks)} 檔"
          f"(相抵金額≥{args.min_amount}億、佔量≥{args.min_pct}%、"
          f"對稱度<{args.max_asym}%、振幅≥{args.min_range}%)")
    OUT.mkdir(parents=True, exist_ok=True)
    token = get_token()

    index = []
    for i, r in enumerate(picks, 1):
        sid = r["stock_id"]
        mine, market, ticks, after, raw_n = build_stock(token, sid, date, args.bno)

        # 互驗:逐價位加總必須等於 broker_daily 既有的買賣股數。對不上表示
        # 抓到的不是同一天,或分點代號在兩邊的寫法不同 —— 停下來,不要出圖。
        gb, gs = sum(x["b"] for x in mine), sum(x["s"] for x in mine)
        if (gb, gs) != (r["buy_sh"], r["sell_sh"]):
            sys.exit(f"{sid} 逐價位加總 買{gb}/賣{gs} 與 broker_daily "
                     f"買{r['buy_sh']}/賣{r['sell_sh']} 不符,中止")

        (OUT / f"{sid}.json").write_text(json.dumps({
            "stock_id": sid, "name": names.get(sid, ""), "date": date,
            "bno": args.bno, "broker_name": bname,
            "quote": {k: r[k] for k in
                      ("open", "high", "low", "close", "spread",
                       "volume", "amount", "prev_close", "range_pct",
                       "chg_pct")},
            "metrics": {k: r[k] for k in
                        ("dt_sh", "dt_amount", "dt_pct", "asym_pct", "net_sh",
                         "dt_pnl", "dt_roi", "part_pct", "buy_sh", "sell_sh",
                         "buy_vwap", "sell_vwap")},
            "ticks": ticks, "ticks_after": after,
            "broker_price": mine, "market_price": market,
        }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        index.append({"stock_id": sid, "name": names.get(sid, ""),
                      **{k: r[k] for k in
                         ("dt_sh", "dt_amount", "dt_pct", "asym_pct", "net_sh",
                          "dt_pnl", "dt_roi", "part_pct", "volume", "amount",
                          "close", "spread", "prev_close", "range_pct",
                          "chg_pct")},
                      "ticks": len(ticks), "prices": len(mine)})
        kb = (OUT / f"{sid}.json").stat().st_size / 1024
        print(f"  [{i}/{len(picks)}] {sid} {names.get(sid,''):　<6}"
              f"逐筆 {len(ticks):>6} 筆 / 逐價位 {raw_n:>6} 列"
              f"(該分點 {len(mine)} 個價位) → {kb:,.0f} KB")
        time.sleep(1)

    (OUT / "index.json").write_text(json.dumps({
        "date": date, "bno": args.bno, "broker_name": bname,
        "params": {"min_amount": args.min_amount, "min_pct": args.min_pct,
                   "max_asym": args.max_asym, "min_range": args.min_range},
        "stocks": index,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    total = sum(f.stat().st_size for f in OUT.glob("*.json")) / 1024 / 1024
    print(f"完成:{len(index)} 檔,web/demo/ 共 {total:.1f} MB")


if __name__ == "__main__":
    main()
