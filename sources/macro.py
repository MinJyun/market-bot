"""資料源:國際總經指標(匯率/美元指數/原油/黃金/美債/VIX/美股)。

台股籌碼的國際對照基準:外資動向與匯率、美元指數連動;費半/S&P500 是
台股(尤其電子)的隔夜引導;VIX 看風險情緒;油金債看資金流與通膨預期。

- 美元/台幣、美元/日圓:期交所「每日外幣參考匯率」(同 pc_ratio,預設頁
  含近兩個月歷史,curl_cffi)。
- 其餘:Yahoo Finance chart API(免金鑰),每次抓近一個月日線順帶回補。
  美股相關數值為台北時間清晨的美國收盤(或最新盤中),與台股資料日相差
  一天屬正常。

fetch 即回補,不需 backfill。取代早期只有美元/台幣的 fx 來源(fx 表已併入)。
對外契約:NAME / fetch(conn) / build_message(conn)。
"""
import json
import re

import requests

from core import store, taifex

NAME = "macro"
FX_URL = "https://www.taifex.com.tw/cht/3/dailyFXRate"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"}

# (symbol, Yahoo 代號, 顯示名, 小數位, 後綴)
YAHOO_SERIES = [
    ("DXY", "DX-Y.NYB", "美元指數", 2, ""),
    ("WTI", "CL=F", "WTI原油", 2, ""),
    ("GOLD", "GC=F", "黃金", 0, ""),
    ("US10Y", "^TNX", "美債10Y", 2, "%"),
    ("VIX", "^VIX", "VIX", 2, ""),
    ("SOX", "^SOX", "費半", 0, ""),
    ("SPX", "^GSPC", "S&P500", 0, ""),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS macro (
    data_date TEXT NOT NULL,   -- YYYY-MM-DD(各序列自己的交易日)
    symbol    TEXT NOT NULL,
    value     REAL,
    PRIMARY KEY (data_date, symbol)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _fetch_fx(conn):
    """期交所匯率頁:美元/台幣(第2欄)、美元/日幣(第5欄)。"""
    html = taifex.get(FX_URL)
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cs = [re.sub(r"<[^>]+>|\s", "", c)
              for c in re.split(r"(?i)<td[^>]*>", tr)[1:]]
        if len(cs) >= 5 and re.match(r"^\d{4}/\d{2}/\d{2}$", cs[0]):
            dd = cs[0].replace("/", "-")
            rows.append((dd, "USDTWD", float(cs[1])))
            rows.append((dd, "USDJPY", float(cs[4])))
    if not rows:
        raise RuntimeError("匯率頁解析失敗,期交所可能改版")
    with conn:
        conn.executemany("INSERT OR REPLACE INTO macro VALUES (?,?,?)", rows)
    latest = max(dd for dd, _, _ in rows)
    store.save_raw(NAME, latest, "dailyFXRate", "html", html.encode("utf-8"))
    return len(rows) // 2


def _fetch_yahoo(conn, symbol, ticker):
    r = requests.get(YAHOO.format(ticker),
                     params={"range": "1mo", "interval": "1d"},
                     headers=UA, timeout=20)
    r.raise_for_status()
    j = r.json()["chart"]["result"][0]
    closes = j["indicators"]["quote"][0]["close"]
    from datetime import datetime
    rows = [(datetime.fromtimestamp(t).strftime("%Y-%m-%d"), symbol, c)
            for t, c in zip(j["timestamp"], closes) if c is not None]
    if not rows:
        raise RuntimeError("無資料")
    with conn:
        conn.executemany("INSERT OR REPLACE INTO macro VALUES (?,?,?)", rows)
    store.save_raw(NAME, rows[-1][0], f"yahoo_{symbol}", "json", r.content)
    return rows[-1][0]


def fetch(conn):
    fails = []
    try:
        n = _fetch_fx(conn)
        print(f"[fetch] macro 匯率: {n} 個交易日")
    except Exception as e:
        fails.append("fx")
        print(f"[fetch] macro 匯率: 失敗 — {e}")
    for symbol, ticker, name, *_ in YAHOO_SERIES:
        try:
            _fetch_yahoo(conn, symbol, ticker)
        except Exception as e:
            fails.append(symbol)
            print(f"[fetch] macro {name}: 失敗 — {e}")
    if fails:
        print(f"[fetch] macro: {len(YAHOO_SERIES) + 1 - len(fails)} 個序列成功,"
              f"失敗:{','.join(fails)}")
    return ["macro"] if len(fails) > len(YAHOO_SERIES) // 2 else []


def _latest2(conn, symbol):
    return conn.execute(
        "SELECT data_date, value FROM macro WHERE symbol=? "
        "ORDER BY data_date DESC LIMIT 2", (symbol,)).fetchall()


def build_message(conn):
    from datetime import datetime
    # 只在晚間時段發(21:30 備援排程):18:00 時美股/油金等尚未更新完
    if datetime.now().hour < 21:
        return None, {}
    lines, sig = [], {}

    def add(symbol, name, dec, suffix="", pct=True, extra=""):
        rows = _latest2(conn, symbol)
        if not rows:
            return
        (dd, v), prev = rows[0], (rows[1][1] if len(rows) > 1 else None)
        sig[symbol] = dd
        if prev:
            chg = (f"（{(v - prev) / prev * 100:+.2f}%{extra}）" if pct else
                   f"（{v - prev:+,.{dec}f}{extra}）")
        else:
            chg = ""
        lines.append(f"{name} {v:,.{dec}f}{suffix}{chg}")

    rows = _latest2(conn, "USDTWD")
    trend = ""
    if len(rows) > 1 and rows[0][1] != rows[1][1]:
        trend = ",台幣貶" if rows[0][1] > rows[1][1] else ",台幣升"
    add("USDTWD", "美元/台幣", 3, extra=trend)
    add("USDJPY", "美元/日圓", 2)
    for symbol, _, name, dec, suffix in YAHOO_SERIES:
        # 美債殖利率本身是 %,漲跌用絕對值(百分點)而非 %
        add(symbol, name, dec, suffix, pct=(symbol != "US10Y"))
    if not lines:
        return None, {}
    dd = sig.get("USDTWD") or max(sig.values())
    header = f"🌍 國際總經 {dd[5:].replace('-', '/')}(美股為前一收盤)"
    return "\n".join([header] + lines), sig
