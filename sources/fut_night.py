"""資料源:台指期夜盤(期交所盤後交易時段)。

期交所「盤後交易時段」15:00 開盤、次日凌晨 5:00 收盤,成交歸入**次一交易日**
的日期——所以早上 8 點抓到的「今日盤後」列,就是剛結束的夜盤。漲跌基準為
前一日結算價。夜盤反映歐美盤時段的台股預期,搭配國際總經在早上推播。

行情取 futDataDown CSV「盤後」列(近月=成交量最大月份);三大法人取夜盤
專頁 futContractsDateAhExcel 的「交易口數與契約金額」——夜盤只有買賣淨額
(flow),未平倉部位是日級口徑、併入 futures_traders 的日盤頁。皆 curl_cffi。
fetch 順帶回補近幾日,不需 backfill。
對外契約:NAME / fetch(conn) / build_message(conn)。
"""
import csv
import io
from datetime import date, timedelta

from core import store, taifex

NAME = "fut_night"
URL = "https://www.taifex.com.tw/cht/3/futDataDown"
INST_URL = "https://www.taifex.com.tw/cht/3/futContractsDateAhExcel"
INST_CONTRACT = "臺股期貨"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fut_night (
    data_date TEXT PRIMARY KEY,   -- 歸屬交易日(夜盤於當日凌晨收盤)
    month     TEXT,               -- 取用的契約月份(成交量最大=近月)
    open REAL, high REAL, low REAL, close REAL,
    chg REAL, chg_pct REAL,       -- 漲跌(vs 前一日結算價)
    volume REAL
);
CREATE TABLE IF NOT EXISTS fut_night_inst (
    data_date TEXT PRIMARY KEY,   -- 歸屬交易日
    foreign_net REAL, trust_net REAL, dealer_net REAL   -- 夜盤交易淨額(口)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def fetch(conn):
    fails = []
    # 1) 夜盤行情(近月開高低收/量)
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
            raise RuntimeError("查無夜盤行情")
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fut_night VALUES (?,?,?,?,?,?,?,?,?)",
                [r for _, r in best.values()])
        latest = max(best)
        store.save_raw(NAME, latest, "futDataDown", "csv", raw)
        print(f"[fetch] fut_night 行情: {len(best)} 日(最新 {latest})")
    except Exception as e:
        fails.append("price")
        print(f"[fetch] fut_night 行情: 失敗 — {e}")
    # 2) 夜盤三大法人交易淨額(臺股期貨)
    try:
        html = taifex.get(INST_URL)
        dd = taifex.inst_date(html)
        who = taifex.parse_inst_flow(html).get(INST_CONTRACT) or {}
        if not dd or not who:
            raise RuntimeError("夜盤三大法人頁解析失敗")
        store.save_raw(NAME, dd, "futContractsAh", "html", html.encode("utf-8"))
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO fut_night_inst VALUES (?,?,?,?)",
                (dd, who.get("外資", 0), who.get("投信", 0),
                 who.get("自營商", 0)))
        print(f"[fetch] fut_night 三大法人: 資料日 {dd}")
    except Exception as e:
        fails.append("inst")
        print(f"[fetch] fut_night 三大法人: 失敗 — {e}")
    return ["fut_night"] if len(fails) == 2 else []


def build_message(conn):
    row = conn.execute(
        "SELECT data_date, close, chg, chg_pct, high, low, volume "
        "FROM fut_night ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return None, {}
    dd, close, chg, pct, high, low, vol = row
    lines = [
        f"🌙 台指期夜盤 {dd[5:].replace('-', '/')}(凌晨5點收盤)",
        f"收盤 {close:,.0f}（{chg:+,.0f},{pct:+.2f}%）",
        f"高 {high:,.0f} 低 {low:,.0f},成交 {vol:,.0f} 口",
    ]
    inst = conn.execute(
        "SELECT foreign_net, trust_net, dealer_net FROM fut_night_inst "
        "WHERE data_date=?", (dd,)).fetchone()
    if inst:
        fo, tr, de = inst
        lines.append(f"三大法人夜盤淨額:外資{fo:+,.0f} 投信{tr:+,.0f} "
                     f"自營{de:+,.0f}")
    return "\n".join(lines), {"date": dd, "inst": bool(inst)}
