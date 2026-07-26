"""資料源:融資融券餘額 + 借券賣出餘額(TWSE,全市場)。

兩個 endpoint 合成一則「放空/槓桿全貌」訊息:

- MI_MARGN「信用交易統計」彙總表:全市場融資(散戶多方槓桿)、融券(散戶
  放空)每日買進/賣出/償還/餘額(張),以及融資金額(仟元)——張數看部位、
  金額看資金水位。
- TWT93U「融券借券賣出餘額」:逐股彙總全市場借券賣出餘額(股)。外資放空
  主要走借券而非融券,補融券看不到的放空力道。

TWSE 開放 JSON、無 bot 防護,標準 requests 即可。可回補歷史。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
from datetime import date

import requests

from core import store
from core.twse import UA, backfill_days, fetch_recent, iso, to_int

NAME = "margin"
URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
SBL_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U"

SCHEMA = """
CREATE TABLE IF NOT EXISTS margin (
    data_date    TEXT PRIMARY KEY,
    margin_buy REAL, margin_sell REAL, margin_redeem REAL,
    margin_prev REAL, margin_bal REAL,
    short_buy  REAL, short_sell  REAL, short_redeem  REAL,
    short_prev REAL, short_bal  REAL,
    amt_buy REAL, amt_sell REAL, amt_redeem REAL,
    amt_prev REAL, amt_bal REAL,                     -- 融資金額(仟元)
    sbl_prev REAL, sbl_bal REAL,                     -- 借券賣出餘額(股)
    fetched_at TEXT
);
"""

COLS = ("data_date, margin_buy, margin_sell, margin_redeem, margin_prev,"
        " margin_bal, short_buy, short_sell, short_redeem, short_prev,"
        " short_bal, amt_buy, amt_sell, amt_redeem, amt_prev, amt_bal,"
        " sbl_prev, sbl_bal, fetched_at")


def init(conn):
    conn.executescript(SCHEMA)


def _fetch_day(d: date):
    """回傳 (data_date, 融資張, 融券張, 融資金額, 借券(前日,今日), raws) 或 None。"""
    resp = requests.get(URL, params={"date": f"{d:%Y%m%d}", "selectType": "ALL",
                                     "response": "json"}, headers=UA, timeout=20)
    resp.raise_for_status()
    j = resp.json()
    if j.get("stat") != "OK" or not j.get("tables"):
        return None
    summary = next((t for t in j["tables"] if "信用交易統計" in t.get("title", "")),
                   None)
    if not summary:
        return None
    rows = {row[0]: [to_int(x) for x in row[1:]] for row in summary["data"]}
    m, s = rows.get("融資(交易單位)"), rows.get("融券(交易單位)")
    a = rows.get("融資金額(仟元)")
    if not m or not s or not a:
        return None
    # 借券賣出餘額(TWT93U 逐股,單位股):加總前日/當日餘額
    r2 = requests.get(SBL_URL, params={"date": f"{d:%Y%m%d}",
                                       "response": "json"}, headers=UA, timeout=30)
    r2.raise_for_status()
    j2 = r2.json()
    if j2.get("stat") != "OK" or not j2.get("data"):
        return None
    sbl = (sum(to_int(row[8]) for row in j2["data"]),
           sum(to_int(row[12]) for row in j2["data"]))
    dd = iso(j.get("date", f"{d:%Y%m%d}"))
    return dd, m, s, a, sbl, (resp.content, r2.content)


def _save(conn, got):
    dd, m, s, a, sbl, (raw, raw2) = got
    store.save_raw(NAME, dd, "MI_MARGN", "json", raw)
    store.save_raw(NAME, dd, "TWT93U", "json", raw2)
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO margin ({COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dd, *m, *s, *a, *sbl, store.now()))


def fetch(conn):
    return fetch_recent(conn, NAME, _fetch_day, _save)


def backfill(conn, days):
    backfill_days(conn, NAME, _fetch_day, _save, days)


def build_message(conn):
    row = conn.execute(
        "SELECT data_date, margin_bal, margin_prev, short_bal, short_prev, "
        "amt_bal, amt_prev, sbl_bal, sbl_prev "
        "FROM margin ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return None, {}
    dd, mbal, mprev, sbal, sprev, abal, aprev, bbal, bprev = row

    def line(name, bal, prev, unit="張"):
        return f"{name} {bal:,.0f}{unit}（較前日{bal - prev:+,.0f}）"

    lines = [
        f"📊 融資融券／借券 {dd[5:].replace('-', '/')}",
        line("融資餘額", mbal, mprev),
    ]
    if abal:  # 仟元 → 億
        lines.append(f"融資金額 {abal / 1e5:,.1f}億"
                     f"（較前日{(abal - aprev) / 1e5:+,.1f}億）")
    lines.append(line("融券餘額", sbal, sprev))
    if bbal:  # 股 → 張
        lines.append(line("借券賣出餘額", bbal / 1000, bprev / 1000))
    return "\n".join(lines), {"date": dd}
