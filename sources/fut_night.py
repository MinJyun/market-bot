"""資料源:台指期夜盤(期交所盤後交易時段)。

期交所「盤後交易時段」15:00 開盤、次日凌晨 5:00 收盤,成交歸入**次一交易日**
的日期——所以早上 8 點抓到的「今日盤後」列,就是剛結束的夜盤。漲跌基準為
前一日結算價。夜盤反映歐美盤時段的台股預期,搭配國際總經在早上推播。

資料同 futures_traders 的 futDataDown CSV(取「盤後」列、近月=成交量最大
月份),curl_cffi。fetch 順帶回補近幾日,不需 backfill。
對外契約:NAME / fetch(conn) / build_message(conn)。
"""
import csv
import io
from datetime import date, timedelta

from core import store, taifex

NAME = "fut_night"
URL = "https://www.taifex.com.tw/cht/3/futDataDown"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fut_night (
    data_date TEXT PRIMARY KEY,   -- 歸屬交易日(夜盤於當日凌晨收盤)
    month     TEXT,               -- 取用的契約月份(成交量最大=近月)
    open REAL, high REAL, low REAL, close REAL,
    chg REAL, chg_pct REAL,       -- 漲跌(vs 前一日結算價)
    volume REAL
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def fetch(conn):
    try:
        start = date.today() - timedelta(days=4)
        raw = taifex.get_bytes(URL, data={
            "down_type": "1", "commodity_id": "TX",
            "queryStartDate": f"{start:%Y/%m/%d}",
            "queryEndDate": f"{date.today():%Y/%m/%d}"})
        text = raw.decode("big5", errors="replace")
        best = {}  # dd -> (volume, row)
        for row in csv.reader(io.StringIO(text)):
            if (len(row) < 18 or row[1].strip() != "TX"
                    or row[17].strip() != "盤後"
                    or not row[2].strip().isdigit()):
                continue
            try:
                vals = [float(str(x).replace(",", "").replace("%", ""))
                        for x in (row[3], row[4], row[5], row[6],
                                  row[7], row[8], row[9])]
            except ValueError:      # 無成交(欄位為 '-')
                continue
            dd = row[0].strip().replace("/", "-")
            if dd not in best or vals[6] > best[dd][0]:
                best[dd] = (vals[6], (dd, row[2].strip(), *vals))
        if not best:
            print("[fetch] fut_night: 查無夜盤資料")
            return ["fut_night"]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fut_night VALUES (?,?,?,?,?,?,?,?,?)",
                [r for _, r in best.values()])
        latest = max(best)
        store.save_raw(NAME, latest, "futDataDown", "csv", raw)
        print(f"[fetch] fut_night: {len(best)} 日(最新 {latest})")
        return []
    except Exception as e:
        print(f"[fetch] fut_night: 失敗 — {e}")
        return ["fut_night"]


def build_message(conn):
    row = conn.execute(
        "SELECT data_date, close, chg, chg_pct, high, low, volume "
        "FROM fut_night ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return None, {}
    dd, close, chg, pct, high, low, vol = row
    text = "\n".join([
        f"🌙 台指期夜盤 {dd[5:].replace('-', '/')}(凌晨5點收盤)",
        f"收盤 {close:,.0f}（{chg:+,.0f},{pct:+.2f}%）",
        f"高 {high:,.0f} 低 {low:,.0f},成交 {vol:,.0f} 口",
    ])
    return text, {"date": dd}
