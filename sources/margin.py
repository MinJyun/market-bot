"""資料源:融資融券餘額 + 借券賣出餘額(TWSE,全市場)。

兩個 endpoint 合成一則「放空/槓桿全貌」訊息:

- MI_MARGN「信用交易統計」彙總表:全市場融資(散戶多方槓桿)、融券(散戶
  放空)每日買進/賣出/償還/餘額(張),以及融資金額(仟元)——張數看部位、
  金額看資金水位。
- TWT93U「融券借券賣出餘額」:逐股彙總全市場借券賣出餘額(股)。外資放空
  主要走借券而非融券,補融券看不到的放空力道。

除全市場彙總外,逐股資券/借券餘額也入庫(margin_stock 表,同一回應內容、
零額外請求),供 my_chips 交叉個人持股。

另抓 MI_INDEX(ALLBUT0999)全部個股收盤價(stock_close 表),用來估算
**大盤融資維持率** = Σ(每檔融資餘額×收盤價) ÷ 全市場融資金額 × 100%。
交易所不公布整戶維持率,這是媒體常用的估算口徑(上市;忽略現金增提擔保
與當日無成交個股),趨勢參考用。低於 ~150% 常伴隨斷頭賣壓。

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
QUOTE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"

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
CREATE TABLE IF NOT EXISTS margin_stock (
    data_date  TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT,
    fin_prev   REAL, fin_bal   REAL,                 -- 融資餘額(張)
    short_prev REAL, short_bal REAL,                 -- 融券餘額(張)
    sbl_prev   REAL, sbl_bal   REAL,                 -- 借券賣出餘額(股)
    PRIMARY KEY (data_date, code)
);
CREATE TABLE IF NOT EXISTS stock_close (
    data_date  TEXT NOT NULL,
    code       TEXT NOT NULL,
    close      REAL,                                 -- 收盤價(上市)
    PRIMARY KEY (data_date, code)
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
    # 全部個股收盤價(維持率估算用);當日無成交(收盤價 "--")跳過
    r3 = requests.get(QUOTE_URL, params={"date": f"{d:%Y%m%d}",
                                         "type": "ALLBUT0999",
                                         "response": "json"},
                      headers=UA, timeout=60)
    r3.raise_for_status()
    quote = next((t for t in r3.json().get("tables", [])
                  if "每日收盤行情" in (t.get("title") or "")), None)
    closes = {}
    for row in (quote or {}).get("data", []):
        try:
            closes[row[0].strip()] = float(str(row[8]).replace(",", ""))
        except ValueError:
            continue
    # 逐股:{code: [name, 融資前日, 融資今日, 融券前日, 融券今日, 借券前日, 借券今日]}
    stocks = {}
    detail = next((t for t in j["tables"] if "融資融券彙總" in t.get("title", "")),
                  None)
    for row in (detail or {}).get("data", []):
        stocks[row[0]] = [row[1].strip(), to_int(row[5]), to_int(row[6]),
                          to_int(row[11]), to_int(row[12]), 0, 0]
    for row in j2["data"]:
        entry = stocks.setdefault(row[0], [row[1].strip(), 0, 0, 0, 0, 0, 0])
        entry[5], entry[6] = to_int(row[8]), to_int(row[12])
    dd = iso(j.get("date", f"{d:%Y%m%d}"))
    return dd, m, s, a, sbl, stocks, closes, (resp.content, r2.content, r3.content)


def _save(conn, got):
    dd, m, s, a, sbl, stocks, closes, (raw, raw2, raw3) = got
    store.save_raw(NAME, dd, "MI_MARGN", "json", raw)
    store.save_raw(NAME, dd, "TWT93U", "json", raw2)
    store.save_raw(NAME, dd, "MI_INDEX_ALL", "json", raw3)
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO margin ({COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dd, *m, *s, *a, *sbl, store.now()))
        conn.execute("DELETE FROM margin_stock WHERE data_date=?", (dd,))
        conn.executemany(
            "INSERT INTO margin_stock VALUES (?,?,?,?,?,?,?,?,?)",
            [(dd, code, *vals) for code, vals in stocks.items()])
        conn.execute("DELETE FROM stock_close WHERE data_date=?", (dd,))
        conn.executemany("INSERT INTO stock_close VALUES (?,?,?)",
                         [(dd, code, c) for code, c in closes.items()])


def fetch(conn):
    return fetch_recent(conn, NAME, _fetch_day, _save)


def backfill(conn, days):
    backfill_days(conn, NAME, _fetch_day, _save, days)


def _maintenance_ratio(conn, dd):
    """估算大盤融資維持率(%):Σ(融資餘額×收盤價) / 融資金額;缺資料回 None。"""
    collateral = conn.execute(
        "SELECT SUM(ms.fin_bal * 1000 * sc.close) FROM margin_stock ms "
        "JOIN stock_close sc ON sc.data_date = ms.data_date AND sc.code = ms.code "
        "WHERE ms.data_date = ?", (dd,)).fetchone()[0]
    amt = conn.execute("SELECT amt_bal FROM margin WHERE data_date=?",
                       (dd,)).fetchone()
    if not collateral or not amt or not amt[0]:
        return None
    return collateral / (amt[0] * 1000) * 100


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
    ratio = _maintenance_ratio(conn, dd)
    if ratio:
        prev_dd = conn.execute(
            "SELECT MAX(data_date) FROM margin WHERE data_date < ?",
            (dd,)).fetchone()[0]
        pratio = _maintenance_ratio(conn, prev_dd) if prev_dd else None
        chg = f"（較前日{ratio - pratio:+.1f}）" if pratio else ""
        lines.append(f"融資維持率(估) {ratio:.1f}%{chg}")
    lines.append(line("融券餘額", sbal, sprev))
    if bbal:  # 股 → 張
        lines.append(line("借券賣出餘額", bbal / 1000, bprev / 1000))
    return "\n".join(lines), {"date": dd}
