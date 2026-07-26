"""資料源:美元/新台幣匯率(期交所每日外幣參考匯率)。

外資買賣超與台幣匯率高度連動——台幣升值常伴隨外資匯入買股、貶值伴隨匯出。
放在籌碼訊息中當資金流向的對照基準。

頁面 https://www.taifex.com.tw/cht/3/dailyFXRate 預設(免帶查詢參數)即回傳
近兩個月的交易日歷史表,故 fetch 每次執行就順帶回補,不需另外實作 backfill。
期交所有 bot 防護,用 curl_cffi(見 core/taifex.py)。
對外契約:NAME / fetch(conn) / build_message(conn)。
"""
import re

from core import store, taifex

NAME = "fx"
URL = "https://www.taifex.com.tw/cht/3/dailyFXRate"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fx (
    data_date  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    usd_twd    REAL,               -- 美元/新台幣
    fetched_at TEXT
);
"""

ROW_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})</td>\s*<td[^>]*>([\d.]+)")


def init(conn):
    conn.executescript(SCHEMA)


def fetch(conn):
    try:
        html = taifex.get(URL)
        rows = ROW_RE.findall(html)
        if not rows:
            print("[fetch] fx: 頁面解析失敗,期交所可能改版")
            return ["fx"]
        now = store.now()
        with conn:
            for y, m, d, rate in rows:
                conn.execute("INSERT OR REPLACE INTO fx VALUES (?,?,?)",
                             (f"{y}-{m}-{d}", float(rate), now))
        latest = max(f"{y}-{m}-{d}" for y, m, d, _ in rows)
        store.save_raw(NAME, latest, "dailyFXRate", "html",
                       html.encode("utf-8"))
        print(f"[fetch] fx: {len(rows)} 個交易日(最新 {latest})")
        return []
    except Exception as e:
        print(f"[fetch] fx: 失敗 — {e}")
        return ["fx"]


def build_message(conn):
    rows = conn.execute(
        "SELECT data_date, usd_twd FROM fx "
        "ORDER BY data_date DESC LIMIT 2").fetchall()
    if not rows:
        return None, {}
    dd, rate = rows[0]
    text = f"💱 美元/台幣 {dd[5:].replace('-', '/')}:{rate:.3f}"
    if len(rows) > 1:
        chg = rate - rows[1][1]
        trend = "台幣貶" if chg > 0 else ("台幣升" if chg < 0 else "持平")
        text += f"（{chg:+.3f},{trend}）"
    return text, {"date": dd}
