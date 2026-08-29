"""資料源:台指期夜盤(期交所盤後交易時段)。

期交所「盤後交易時段」15:00 開盤、次日凌晨 5:00 收盤,成交歸入**次一交易日**
的日期——所以早上 8 點抓到的「今日盤後」列,就是剛結束的夜盤。漲跌基準為
前一日結算價。夜盤反映歐美盤時段的台股預期,搭配國際總經在早上推播。

三塊資料(皆 curl_cffi;夜盤法人只公布交易買賣淨額 flow、無未平倉,
部位是日級口徑、在 futures_traders):
- 行情:futDataDown CSV「盤後」列,近月(成交量最大月份)開高低收/量。
- 期貨三大法人:futContractsDateAhExcel,台指/小台/微台交易淨額(口),
  訊息另算「合併」= 台指 + 0.25×小台 + 0.05×微台(依契約規模折算)。
- 選擇權三大法人:callsAndPutsDateAhExcel,臺指選擇權 Call/Put 交易淨額。

fetch 順帶回補近幾日行情,不需 backfill。
對外契約:NAME / fetch(conn) / build_message(conn)。
"""
import csv
import io
from datetime import date, timedelta

from core import store, taifex

NAME = "fut_night"
URL = "https://www.taifex.com.tw/cht/3/futDataDown"
INST_URL = "https://www.taifex.com.tw/cht/3/futContractsDateAhExcel"
OPT_URL = "https://www.taifex.com.tw/cht/3/callsAndPutsDateAhExcel"
OPT_CONTRACT = "臺指選擇權"

# 期交所契約名 → (顯示名, 合併權重:相對台指的契約規模)
CONTRACTS = {
    "臺股期貨": ("台指", 1.0),
    "小型臺指期貨": ("小台", 0.25),
    "微型臺指期貨": ("微台", 0.05),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fut_night (
    data_date TEXT PRIMARY KEY,   -- 歸屬交易日(夜盤於當日凌晨收盤)
    month     TEXT,               -- 取用的契約月份(成交量最大=近月)
    open REAL, high REAL, low REAL, close REAL,
    chg REAL, chg_pct REAL,       -- 漲跌(vs 前一日結算價)
    volume REAL
);
CREATE TABLE IF NOT EXISTS fut_night_inst (
    data_date TEXT NOT NULL,      -- 歸屬交易日
    contract  TEXT NOT NULL,      -- 台指/小台/微台
    foreign_net REAL, trust_net REAL, dealer_net REAL,  -- 夜盤交易淨額(口)
    PRIMARY KEY (data_date, contract)
);
CREATE TABLE IF NOT EXISTS fut_night_opt (
    data_date TEXT NOT NULL,
    cp        TEXT NOT NULL,      -- 買權 / 賣權
    foreign_net REAL, trust_net REAL, dealer_net REAL,  -- 夜盤交易淨額(口)
    PRIMARY KEY (data_date, cp)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def fetch(conn):
    fails = []
    # 1) 夜盤行情(近月開高低收/量)
    try:
        # 週五晚的夜盤歸屬「次一交易日」(下週一),查詢範圍需延伸到未來
        # 幾天,週六早上才抓得到剛收盤的那節
        start = date.today() - timedelta(days=4)
        raw = taifex.get_bytes(URL, data={
            "down_type": "1", "commodity_id": "TX",
            "queryStartDate": f"{start:%Y/%m/%d}",
            "queryEndDate": f"{date.today() + timedelta(days=4):%Y/%m/%d}"})
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
    # 2) 期貨三大法人夜盤交易淨額(台指/小台/微台)
    try:
        html = taifex.get(INST_URL)
        dd = taifex.inst_date(html)
        flow = taifex.parse_inst_flow(html)
        rows = [(dd, disp, w.get("外資", 0), w.get("投信", 0), w.get("自營商", 0))
                for name, (disp, _) in CONTRACTS.items()
                if (w := flow.get(name))]
        if not dd or not rows:
            raise RuntimeError("期貨夜盤三大法人頁解析失敗")
        store.save_raw(NAME, dd, "futContractsAh", "html", html.encode("utf-8"))
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fut_night_inst VALUES (?,?,?,?,?)", rows)
        print(f"[fetch] fut_night 期貨法人: 資料日 {dd},{len(rows)} 契約")
    except Exception as e:
        fails.append("inst")
        print(f"[fetch] fut_night 期貨法人: 失敗 — {e}")
    # 3) 選擇權三大法人夜盤交易淨額(Call/Put)
    try:
        html = taifex.get(OPT_URL)
        dd = taifex.inst_date(html)
        cp = taifex.parse_inst_flow_cp(html).get(OPT_CONTRACT) or {}
        if not dd or not cp:
            raise RuntimeError("選擇權夜盤三大法人頁解析失敗")
        store.save_raw(NAME, dd, "callsAndPutsAh", "html", html.encode("utf-8"))
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fut_night_opt VALUES (?,?,?,?,?)",
                [(dd, side, w.get("外資", 0), w.get("投信", 0),
                  w.get("自營商", 0)) for side, w in cp.items()])
        print(f"[fetch] fut_night 選擇權法人: 資料日 {dd}")
    except Exception as e:
        fails.append("opt")
        print(f"[fetch] fut_night 選擇權法人: 失敗 — {e}")
    return ["fut_night"] if len(fails) == 3 else []


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

    def who_fmt(fo, tr, de):
        return f"外資{fo:+,.0f} 投信{tr:+,.0f} 自營{de:+,.0f}"

    inst = {r[0]: r[1:] for r in conn.execute(
        "SELECT contract, foreign_net, trust_net, dealer_net "
        "FROM fut_night_inst WHERE data_date=?", (dd,))}
    if inst:
        lines.append("▍期貨夜盤淨額(口)")
        merged = [0.0, 0.0, 0.0]
        for name, (disp, weight) in CONTRACTS.items():
            if disp not in inst:
                continue
            vals = inst[disp]
            lines.append(f"　{disp} {who_fmt(*vals)}")
            for i, v in enumerate(vals):
                merged[i] += v * weight
        if len(inst) > 1:
            lines.append(f"　合併 {who_fmt(*merged)}"
                         "(台指+0.25小台+0.05微台)")
    opt = {r[0]: r[1:] for r in conn.execute(
        "SELECT cp, foreign_net, trust_net, dealer_net "
        "FROM fut_night_opt WHERE data_date=?", (dd,))}
    if opt:
        lines.append("▍臺指選擇權夜盤淨額(口)")
        for side, label in (("買權", "Call"), ("賣權", "Put")):
            if side in opt:
                lines.append(f"　{label} {who_fmt(*opt[side])}")
    return "\n".join(lines), {"date": dd, "inst": len(inst), "opt": len(opt)}
