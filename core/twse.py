"""TWSE 開放 JSON 資料源的共用工具:數字解析、日期轉換、逐日抓取迴圈。

TWSE 這類 endpoint(BFI82U/T86/MI_MARGN/MI_INDEX)都是逐日查詢。各資料源只需
提供 fetch_day(d)(回傳以 data_date 開頭的 tuple,或 None 表該日無資料)與
save(conn, got)(可回傳附加到 log 的字串);「往回找最近交易日」「逐日回補、
跳過週末」的控制流程由這裡統一。
"""
from datetime import date, timedelta

UA = {"User-Agent": "Mozilla/5.0"}


def to_int(s):
    s = str(s).replace(",", "").strip()
    return int(s) if s.lstrip("-").isdigit() else 0


def iso(dd):
    """'YYYYMMDD' → 'YYYY-MM-DD'。"""
    return f"{dd[:4]}-{dd[4:6]}-{dd[6:8]}"


def fetch_recent(conn, name, fetch_day, save, lookback=6):
    """從今天往回找最近一個有資料的交易日並入庫;失敗回 [name],成功回 []。"""
    for i in range(lookback):
        d = date.today() - timedelta(days=i)
        try:
            got = fetch_day(d)
        except Exception as e:
            print(f"[fetch] {name}: 失敗 — {e}")
            return [name]
        if got:
            extra = save(conn, got) or ""
            print(f"[fetch] {name}: 資料日 {got[0]}{extra}")
            return []
    print(f"[fetch] {name}: 近 {lookback} 日查無資料")
    return [name]


def backfill_days(conn, name, fetch_day, save, days):
    """逐日回補 days 天(跳過週末);單日失敗不中斷。"""
    for i in range(1, days + 1):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            got = fetch_day(d)
            if not got:
                continue
            save(conn, got)
            print(f"[backfill] {name} {got[0]}")
        except Exception as e:
            print(f"[backfill] {name} {d}: 失敗 — {e}")
