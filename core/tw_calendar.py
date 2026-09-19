"""台股交易日曆(證交所官方休市日程表)。

早報分版與連假靜默都要知道「哪天有開盤」:週一早報看的是週末變化、
連假期間不推、收假後第一天要涵蓋整段休市期間。週末用 weekday 就能判,
國定假日與補假只能靠證交所公告。

證交所 holidaySchedule 回的表**混了休市日與交易日**(「農曆春節前最後
交易日」「國曆新年開始交易日」是有開盤的),不能整張當休市日用 —
以說明欄的「放假」/「無交易」字樣分辨,原始名稱與說明一併存下來,
日後規則要調整不必重抓。

證交所有路徑級限流(見專案既有教訓),故 ensure() 只在該年尚無資料時抓。
"""
import json
from datetime import date, timedelta

import requests

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"}
URL = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tw_holidays (
    data_date TEXT PRIMARY KEY,   -- YYYY-MM-DD
    name      TEXT,
    note      TEXT,
    closed    INTEGER NOT NULL    -- 1=休市 0=該表列出但仍有交易
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _is_closed(name, note):
    """證交所表中該列是否為休市日。有交易的列會明說「交易」。"""
    text = f"{name}{note}"
    if "無交易" in text or "放假" in text or "休市" in text:
        return True
    if "交易" in text:        # 「…最後交易日」「…開始交易日」
        return False
    return True               # 其餘(如颱風停市公告)保守視為休市


def fetch(conn, year):
    r = requests.get(URL, params={"response": "json", "yy": year},
                     headers=UA, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK" and j.get("stat") != "ok":
        raise RuntimeError(f"證交所回應異常:{j.get('stat')}")
    rows = []
    for d, name, note in (row[:3] for row in j["data"]):
        note = note.replace("\r", "").replace("\n", " ").strip()
        rows.append((d, name, note, int(_is_closed(name, note))))
    if not rows:
        raise RuntimeError("休市表為空")
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tw_holidays VALUES (?,?,?,?)", rows)
    return sum(r[3] for r in rows), len(rows)


def ensure(conn, year):
    """該年尚無資料才抓(證交所有限流)。抓失敗不拋錯,只回 False。"""
    n = conn.execute("SELECT COUNT(*) FROM tw_holidays "
                     "WHERE data_date LIKE ?", (f"{year}-%",)).fetchone()[0]
    if n:
        return True
    try:
        closed, total = fetch(conn, year)
        print(f"[calendar] {year} 年休市日程:{closed} 天休市 / 共 {total} 列")
        return True
    except Exception as e:
        print(f"[calendar] {year} 年休市日程抓取失敗 — {e}")
        return False


def is_trading_day(conn, d):
    """d 是否為台股交易日。週末一律否;國定假日查表。

    表中沒有該年資料時,只能以週末判斷 — 呼叫端應先 ensure()。
    """
    if d.weekday() >= 5:
        return False
    row = conn.execute("SELECT closed FROM tw_holidays WHERE data_date=?",
                       (d.isoformat(),)).fetchone()
    return not (row and row[0])


def is_public_holiday(conn, d):
    """d 是否為國定假日(週末不算) — 連假靜默判斷用。"""
    if d.weekday() >= 5:
        return False
    row = conn.execute("SELECT closed FROM tw_holidays WHERE data_date=?",
                       (d.isoformat(),)).fetchone()
    return bool(row and row[0])


def morning_mode(conn, d):
    """早報在 d 這天該用哪種版型。

    - "skip"  連假靜默:今天休市,且(是國定假日 或 昨天也休市)。
              一般週六不落入此條(昨天週五有開盤,夜盤資料是新的)。
    - "special" 跨越休市:前一交易日不是昨天(週一、連假收假後),
              台指期夜盤空窗,改報休市期間國際市場的變化。
    - "normal" 一般日。
    """
    day = timedelta(days=1)
    if not is_trading_day(conn, d):
        if is_public_holiday(conn, d) or not is_trading_day(conn, d - day):
            return "skip"
    prev = prev_trading_day(conn, d)
    if prev and prev < d - day:
        return "special"
    return "normal"


def prev_trading_day(conn, d):
    """d 之前(不含 d)最近的交易日;往前找最多 30 天,找不到回 None。"""
    for i in range(1, 31):
        p = d - timedelta(days=i)
        if is_trading_day(conn, p):
            return p
    return None
