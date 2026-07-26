"""資料源:期交所期貨的機構籌碼(整合兩張表)。

追蹤台指期/電子期/金融期,一則訊息同時呈現:
  1. 三大法人未平倉「多空淨額」(外資/投信/自營)—— 各身份別的整體淨部位。
  2. 大額交易人「前五大/前十大交易人(特定法人)」淨部位 —— 最大帳戶的集中度。

兩者母體不同:三大法人是把「所有外資(或投信/自營)帳戶」加總;大額交易人的
「特定法人」只看部位最大的前 5/10 個帳戶中的法人。故可能外資整體淨空、但前十大
(多為投信)淨多。擺在一起看才不會誤讀。

資料皆來自期交所,curl_cffi 破 bot 防護。不做 backfill(見下),歷史每日累積。
對外契約:NAME / fetch(conn) / build_message(conn)。

不做 backfill:大額交易人僅當日預設頁提供帶語意 headers、可穩定解析的表格,
指定歷史日期會回另一種凌亂版面;故歷史從今天起每日往前累積。
"""
from core import store, taifex

NAME = "futures_traders"
LT_URL = "https://www.taifex.com.tw/cht/3/largeTraderFutQry"       # 大額交易人
INST_URL = "https://www.taifex.com.tw/cht/3/futContractsDateExcel"  # 三大法人

# 期交所契約名稱(兩張表皆用此名)→ 顯示名。台指期為 TX+MTX/4+TMF/20 合併計。
CONTRACTS = {
    "臺股期貨": "台指期",
    "電子期貨": "電子期",
    "金融期貨": "金融期",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS futures_lt (
    contract   TEXT NOT NULL,
    data_date  TEXT NOT NULL,
    buy5   REAL, buy10  REAL, sell5  REAL, sell10  REAL,        -- 特定法人
    buy5_all REAL, buy10_all REAL, sell5_all REAL, sell10_all REAL,  -- 全部交易人
    oi     REAL,
    fetched_at TEXT,
    PRIMARY KEY (contract, data_date)
);
CREATE TABLE IF NOT EXISTS futures_inst (
    contract   TEXT NOT NULL,
    data_date  TEXT NOT NULL,
    foreign_net REAL, trust_net REAL, dealer_net REAL,          -- 未平倉多空淨額(口)
    fetched_at TEXT,
    PRIMARY KEY (contract, data_date)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


# ================================================================ 抓取
def fetch(conn):
    fails = []
    # 1) 大額交易人
    try:
        html = taifex.get(LT_URL)
        dd = taifex.page_date(html)
        rows = {disp: d for name, disp in CONTRACTS.items()
                if (d := taifex.parse_large_trader(html, name))}
        if not dd or not rows:
            raise RuntimeError("大額交易人頁解析失敗")
        store.save_raw(NAME, dd, "largeTraderFut", "html", html.encode("utf-8"))
        now = store.now()
        with conn:
            for disp, d in rows.items():
                conn.execute(
                    "INSERT OR REPLACE INTO futures_lt VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (disp, dd, d["buy5"], d["buy10"], d["sell5"], d["sell10"],
                     d["buy5_all"], d["buy10_all"], d["sell5_all"],
                     d["sell10_all"], d["oi"], now))
        print(f"[fetch] futures_traders 大額: 資料日 {dd},{len(rows)} 契約")
    except Exception as e:
        fails.append("futures_lt")
        print(f"[fetch] futures_traders 大額: 失敗 — {e}")
    # 2) 三大法人
    try:
        html = taifex.get(INST_URL)
        dd = taifex.inst_date(html)
        inst = {n: w for n, w in taifex.parse_inst(html).items()
                if n in CONTRACTS}
        if not dd or not inst:
            raise RuntimeError("三大法人頁解析失敗")
        store.save_raw(NAME, dd, "futContracts", "html", html.encode("utf-8"))
        now = store.now()
        with conn:
            for name, who in inst.items():
                conn.execute(
                    "INSERT OR REPLACE INTO futures_inst VALUES (?,?,?,?,?,?)",
                    (CONTRACTS[name], dd, who.get("外資", 0), who.get("投信", 0),
                     who.get("自營商", 0), now))
        print(f"[fetch] futures_traders 三大法人: 資料日 {dd},{len(inst)} 契約")
    except Exception as e:
        fails.append("futures_inst")
        print(f"[fetch] futures_traders 三大法人: 失敗 — {e}")
    return ["futures_traders"] if len(fails) == 2 else []


# ================================================================ LINE 訊息
def build_message(conn):
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM futures_lt ORDER BY data_date DESC LIMIT 2")]
    idates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM futures_inst ORDER BY data_date DESC LIMIT 2")]
    if not dates and not idates:
        return None, {}
    curr = dates[0] if dates else idates[0]
    prev = dates[1] if len(dates) > 1 else None
    iprev = idates[1] if len(idates) > 1 else None

    def lt(dd, c):
        return conn.execute("SELECT buy5,buy10,sell5,sell10,oi FROM futures_lt "
                            "WHERE contract=? AND data_date=?", (c, dd)).fetchone()

    def inst(dd, c):
        return conn.execute(
            "SELECT foreign_net,trust_net,dealer_net FROM futures_inst "
            "WHERE contract=? AND data_date=?", (c, dd)).fetchone()

    icurr = idates[0] if idates else None

    def signed(v, chg=None):
        s = f"{v:+,.0f}"
        return s + (f"（前日{chg:+,.0f}）" if chg is not None else "")

    def netfmt(net, chg):
        s = f"{'淨多' if net >= 0 else '淨空'}{abs(net):,.0f}口"
        return s + (f"（前日{chg:+,.0f}）" if chg is not None else "")

    lines = [f"📊 期貨機構籌碼 {curr[5:].replace('-', '/')}"]
    for disp in CONTRACTS.values():
        block = []
        ic = inst(icurr, disp) if icurr else None
        if ic:
            ip = inst(iprev, disp) if iprev else None
            fchg = (ic[0] - ip[0]) if ip else None
            block.append(f"　三大法人淨:外資{signed(ic[0], fchg)} "
                         f"投信{ic[1]:+,.0f} 自營{ic[2]:+,.0f}")
        c = lt(curr, disp)
        if c:
            b5, b10, s5, s10, oi = c
            p = lt(prev, disp) if prev else None
            n5, n10 = b5 - s5, b10 - s10
            c5 = (n5 - (p[0] - p[2])) if p else None
            c10 = (n10 - (p[1] - p[3])) if p else None
            block = [f"▍{disp} 未平倉 {oi:,.0f} 口"] + block
            block.append(f"　大戶前五大 {netfmt(n5, c5)}")
            block.append(f"　大戶前十大 {netfmt(n10, c10)}")
        elif block:
            block = [f"▍{disp}"] + block
        if block:
            lines += block
    return "\n".join(lines), {"lt": curr, "inst": icurr}
