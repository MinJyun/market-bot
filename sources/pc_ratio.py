"""資料源:期交所臺指選擇權 Put/Call Ratio(市場情緒指標)。

追蹤臺指選擇權(TXO)每日賣權/買權的成交量比與未平倉量比(%)。比率越高代表
賣權(Put)相對買權(Call)越多——常被視為避險/偏空訊號;反之偏多。

頁面 https://www.taifex.com.tw/cht/3/pcRatio 預設(免帶查詢參數)即回傳約一個
月的交易日歷史表,故 fetch 每次執行就順帶回補近況,不需另外實作 backfill。
TWSE/TAIFEX 頁面有 bot 防護,用 curl_cffi(impersonate chrome)。
對外契約:NAME / fetch(conn) / build_message(conn)。
"""
import re

from core import store, taifex

NAME = "pc_ratio"
URL = "https://www.taifex.com.tw/cht/3/pcRatio"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pc_ratio (
    data_date  TEXT NOT NULL PRIMARY KEY,
    put_vol    REAL, call_vol REAL, vol_ratio REAL,
    put_oi     REAL, call_oi  REAL, oi_ratio  REAL,
    fetched_at TEXT
);
"""

ROW_RE = re.compile(
    r'<td align="center">(\d{4}/\d{1,2}/\d{1,2})</td>\s*'
    r'<td align="center">([\d,]+)</td>\s*'
    r'<td align="center">([\d,]+)</td>\s*'
    r'<td align="center">([\d.]+)</td>\s*'
    r'<td align="center">\s*([\d,]+)</td>\s*'
    r'<td align="center">\s*([\d,]+)</td>\s*'
    r'<td align="center">([\d.]+)</td>'
)


def init(conn):
    conn.executescript(SCHEMA)


def _to_iso(d):
    y, m, day = d.split("/")
    return f"{y}-{int(m):02d}-{int(day):02d}"


def fetch(conn):
    try:
        html = taifex.get(URL)
        rows = ROW_RE.findall(html)
        if not rows:
            print("[fetch] pc_ratio: 頁面解析失敗,期交所可能改版")
            return ["pc_ratio"]
        now = store.now()
        with conn:
            for d, pv, cv, vr, po, co, oir in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO pc_ratio VALUES (?,?,?,?,?,?,?,?)",
                    (_to_iso(d), float(pv.replace(",", "")),
                     float(cv.replace(",", "")), float(vr),
                     float(po.replace(",", "")), float(co.replace(",", "")),
                     float(oir), now))
        store.save_raw(NAME, _to_iso(rows[0][0]), "pcRatio", "html",
                        html.encode("utf-8"))
        print(f"[fetch] pc_ratio: {len(rows)} 個交易日(最新 {_to_iso(rows[0][0])})")
        return []
    except Exception as e:
        print(f"[fetch] pc_ratio: 失敗 — {e}")
        return ["pc_ratio"]


def build_message(conn):
    rows = conn.execute(
        "SELECT data_date, put_vol, call_vol, vol_ratio, put_oi, call_oi, oi_ratio "
        "FROM pc_ratio ORDER BY data_date DESC LIMIT 2").fetchall()
    if not rows:
        return None, {}
    curr = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    dd, pv, cv, vr, po, co, oir = curr

    def chg(idx):
        return f"（{curr[idx] - prev[idx]:+.2f}）" if prev else ""

    lines = [
        f"📊 臺指選擇權 Put/Call Ratio {dd[5:].replace('-', '/')}",
        f"成交量比 {vr:.2f}%{chg(3)}（賣{pv:,.0f}／買{cv:,.0f}口）",
        f"未平倉比 {oir:.2f}%{chg(6)}（賣{po:,.0f}／買{co:,.0f}口）",
    ]
    return "\n".join(lines), {"date": dd}
