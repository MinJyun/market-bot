"""TWSE 開放 JSON 資料源的共用工具:數字解析、日期轉換、逐日抓取迴圈。

TWSE 這類 endpoint(BFI82U/T86/MI_MARGN/MI_INDEX)都是逐日查詢。各資料源只需
提供 fetch_day(d)(回傳以 data_date 開頭的 tuple,或 None 表該日無資料)與
save(conn, got)(可回傳附加到 log 的字串);「往回找最近交易日」「逐日回補、
跳過週末」的控制流程由這裡統一。

注意:TWSE 有路徑級速率限制,backfill_days 預設節流 1.5 秒(見該函式)。
"""
import time
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


def backfill_days(conn, name, fetch_day, save, days, pace=1.5, max_fail=8):
    """逐日回補 days 天(跳過週末);單日失敗不中斷。

    **必須節流**:TWSE 對單一路徑有速率限制。無間隔連打會回 HTTP 428,
    或直接吐 HTML 讓 json 解析炸成
    "Expecting value: line 1 column 1 (char 0)" —— 兩種都不會說自己被限流。
    2026-09-05 實測 365 天無間隔回補:inst_spot 226/261 失敗、margin 247/248
    失敗。限流是**路徑級**的:同一時刻 BFI82U 已恢復、TWT93U 仍在冷卻。

    `max_fail`:連續失敗這麼多次就中止。繼續打不會拿到資料,只會讓冷卻
    一直發燙(上面那 226 個失敗請求正是這樣把自己鎖在門外)。
    """
    ok = fail = streak = 0
    for i in range(1, days + 1):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            got = fetch_day(d)
            if got:
                save(conn, got)
                ok += 1
                print(f"[backfill] {name} {got[0]}")
            streak = 0
        except Exception as e:
            fail += 1
            streak += 1
            print(f"[backfill] {name} {d}: 失敗 — {e}")
            if streak >= max_fail:
                print(f"[backfill] {name}: 連續 {streak} 次失敗,中止"
                      f"(多半是被限流,冷卻後重跑即可)")
                break
        time.sleep(pace)
    print(f"[backfill] {name}: 成功 {ok} 天,失敗 {fail} 天")
