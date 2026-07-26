"""資料源:融資融券餘額(TWSE 信用交易統計,全市場)。

TWSE MI_MARGN 回應的「信用交易統計」彙總表:全市場融資(散戶多方槓桿)、
融券(放空)每日買進/賣出/償還/餘額,單位為張(交易單位)。融資餘額增加代表
散戶加碼、融券餘額增加代表放空力道增加,是常見的散戶籌碼/情緒指標。

TWSE 開放 JSON、無 bot 防護,標準 requests 即可。可回補歷史。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
from datetime import date

import requests

from core import store
from core.twse import UA, backfill_days, fetch_recent, iso, to_int

NAME = "margin"
URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"

SCHEMA = """
CREATE TABLE IF NOT EXISTS margin (
    data_date    TEXT PRIMARY KEY,
    margin_buy REAL, margin_sell REAL, margin_redeem REAL,
    margin_prev REAL, margin_bal REAL,
    short_buy  REAL, short_sell  REAL, short_redeem  REAL,
    short_prev REAL, short_bal  REAL,
    fetched_at TEXT
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _fetch_day(d: date):
    """回傳 (data_date, 融資[買,賣,償還,前日,今日], 融券[同上], 原始 bytes) 或 None。"""
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
    if not m or not s:
        return None
    return iso(j.get("date", f"{d:%Y%m%d}")), m, s, resp.content


def _save(conn, got):
    dd, m, s, raw = got
    store.save_raw(NAME, dd, "MI_MARGN", "json", raw)
    with conn:
        conn.execute("INSERT OR REPLACE INTO margin VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (dd, *m, *s, store.now()))


def fetch(conn):
    return fetch_recent(conn, NAME, _fetch_day, _save)


def backfill(conn, days):
    backfill_days(conn, NAME, _fetch_day, _save, days)


def build_message(conn):
    row = conn.execute(
        "SELECT data_date, margin_bal, margin_prev, short_bal, short_prev "
        "FROM margin ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return None, {}
    dd, mbal, mprev, sbal, sprev = row

    def line(name, bal, prev):
        return f"{name} {bal:,.0f}張（較前日{bal - prev:+,.0f}）"

    text = "\n".join([
        f"📊 融資融券餘額 {dd[5:].replace('-', '/')}",
        line("融資餘額", mbal, mprev),
        line("融券餘額", sbal, sprev),
    ])
    return text, {"date": dd}
