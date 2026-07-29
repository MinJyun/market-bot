"""資料源:期交所期貨的機構籌碼(整合兩張表)+ 外資台指期成本估算。

追蹤台指期/電子期/金融期,一則訊息同時呈現:
  1. 三大法人未平倉「多空淨額」(外資/投信/自營)—— 各身份別的整體淨部位。
  2. 大額交易人「前五大/前十大交易人(特定法人)」淨部位 —— 最大帳戶的集中度。
  3. 台指期外資「估算成本」:逐日以「淨部位增量 × 當日近月結算價」累積攤入
     (加碼攤入、減碼不動、翻向重置),搭配浮動損益。**這是估算**:期交所
     只公布按結算價計的未平倉市值,不公布成本;盤中成交價、跨月轉倉價差、
     價差單都無法反映,趨勢參考用。

兩者母體不同:三大法人是把「所有外資(或投信/自營)帳戶」加總;大額交易人的
「特定法人」只看部位最大的前 5/10 個帳戶中的法人。故可能外資整體淨空、但前十大
(多為投信)淨多。擺在一起看才不會誤讀。

資料皆來自期交所,curl_cffi 破 bot 防護。
backfill 只回補「三大法人淨部位」(POST queryDate 可查歷史)與「台指期近月
結算價」(futDataDown CSV,30 日一段);大額交易人僅當日預設頁可穩定解析,
仍不可回補、歷史每日累積。
對外契約:NAME / fetch(conn) / backfill(conn, days) / build_message(conn)。
"""
import csv
import io
import time
from datetime import date, timedelta

from core import store, taifex

NAME = "futures_traders"
LT_URL = "https://www.taifex.com.tw/cht/3/largeTraderFutQry"       # 大額交易人
INST_URL = "https://www.taifex.com.tw/cht/3/futContractsDateExcel"  # 三大法人
PRICE_URL = "https://www.taifex.com.tw/cht/3/futDataDown"          # 行情 CSV 下載

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
CREATE TABLE IF NOT EXISTS fut_price (
    data_date  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    month      TEXT,               -- 取用的契約月份(一般時段成交量最大者=近月)
    close      REAL,               -- 收盤價
    settle     REAL                -- 結算價
);
"""


def init(conn):
    conn.executescript(SCHEMA)


# ================================================================ 台指期價格
def _fetch_prices(conn, start: date, end: date):
    """futDataDown 抓 TX 日行情 CSV(區間 ≤30 日),近月結算價入庫;回傳筆數。"""
    raw = taifex.get_bytes(PRICE_URL, data={
        "down_type": "1", "commodity_id": "TX",
        "queryStartDate": f"{start:%Y/%m/%d}", "queryEndDate": f"{end:%Y/%m/%d}"})
    text = raw.decode("big5", errors="replace")
    best = {}  # dd -> (volume, month, close, settle)
    for row in csv.reader(io.StringIO(text)):
        # 交易日期,契約,到期月份,開,高,低,收,漲跌,漲跌%,成交量,結算價,... ,交易時段
        if len(row) < 18 or row[1].strip() != "TX" or row[17].strip() != "一般":
            continue
        month = row[2].strip()
        if not month.isdigit():          # 排除價差組合(如 202608/202609)
            continue
        try:
            vol, close, settle = float(row[9]), float(row[6]), float(row[10])
        except ValueError:
            continue
        dd = row[0].strip().replace("/", "-")
        if dd not in best or vol > best[dd][0]:
            best[dd] = (vol, month, close, settle)
    if best:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fut_price VALUES (?,?,?,?)",
                [(dd, m, c, s) for dd, (v, m, c, s) in best.items()])
        store.save_raw(NAME, max(best), "futDataDown", "csv", raw)
    return len(best)


# ================================================================ 成本估算
def _foreign_cost(conn):
    """外資台指期估算成本:加碼以當日結算價攤入、減碼不動、翻向重置。

    回傳 (成本, 最新結算價, 淨部位口數, 浮動損益億, 起算日) 或 None(資料不足)。
    """
    rows = conn.execute(
        "SELECT i.data_date, i.foreign_net, p.settle FROM futures_inst i "
        "JOIN fut_price p ON p.data_date = i.data_date "
        "WHERE i.contract = '台指期' ORDER BY i.data_date").fetchall()
    if len(rows) < 2:
        return None
    start, pos, cost = rows[0][0], rows[0][1], rows[0][2]
    for dd, net, settle in rows[1:]:
        if pos == 0 or (pos > 0) != (net > 0):     # 空手或翻向:整筆以今日價重置
            cost, start = settle, dd
        elif abs(net) > abs(pos):                   # 同向加碼:增量攤入
            cost = (cost * abs(pos) + settle * (abs(net) - abs(pos))) / abs(net)
        pos = net                                   # 減碼:成本不動
    settle = rows[-1][2]
    pnl = (settle - cost) * pos * 200 / 1e8         # 台指期每點 200 元
    return cost, settle, pos, pnl, start


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
    # 3) 台指期近月結算價(順帶回補近 10 日,自動補假日缺口)
    try:
        n = _fetch_prices(conn, date.today() - timedelta(days=10), date.today())
        print(f"[fetch] futures_traders 價格: {n} 個交易日")
    except Exception as e:
        fails.append("fut_price")
        print(f"[fetch] futures_traders 價格: 失敗 — {e}")
    return ["futures_traders"] if len(fails) == 3 else []


def backfill(conn, days):
    """回補三大法人淨部位(POST 可查歷史)與台指期結算價;大額交易人不可回補。"""
    for i in range(1, days + 1):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            html = taifex.get(INST_URL, data={
                "queryType": "1", "goDay": "", "doQuery": "1",
                "dateaddcnt": "", "queryDate": f"{d:%Y/%m/%d}",
                "commodityId": ""})
            dd = taifex.inst_date(html)
            if dd != d.isoformat():      # 假日查詢會回別天,跳過避免蓋錯日期
                continue
            inst = {n: w for n, w in taifex.parse_inst(html).items()
                    if n in CONTRACTS}
            if not inst:
                continue
            now = store.now()
            with conn:
                for name, who in inst.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO futures_inst VALUES (?,?,?,?,?,?)",
                        (CONTRACTS[name], dd, who.get("外資", 0),
                         who.get("投信", 0), who.get("自營商", 0), now))
            print(f"[backfill] futures_traders 三大法人 {dd}")
        except Exception as e:
            print(f"[backfill] futures_traders {d}: 失敗 — {e}")
        # 0.4s 曾觸發 Cloudflare 對此路徑的 IP 限流(擋數小時),放慢
        time.sleep(2)
    # 結算價:futDataDown 一次最多約 30 日,分段抓
    end = date.today()
    while (date.today() - end).days < days:
        start = max(end - timedelta(days=29),
                    date.today() - timedelta(days=days))
        try:
            n = _fetch_prices(conn, start, end)
            print(f"[backfill] futures_traders 價格 {start}~{end}:{n} 日")
        except Exception as e:
            print(f"[backfill] futures_traders 價格 {start}~{end}: 失敗 — {e}")
        if start <= date.today() - timedelta(days=days):
            break
        end = start - timedelta(days=1)
        time.sleep(0.4)


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

    cost = _foreign_cost(conn)

    lines = [f"📊 期貨機構籌碼 {curr[5:].replace('-', '/')}"]
    for disp in CONTRACTS.values():
        block = []
        ic = inst(icurr, disp) if icurr else None
        if ic:
            ip = inst(iprev, disp) if iprev else None
            fchg = (ic[0] - ip[0]) if ip else None
            block.append(f"　三大法人淨:外資{signed(ic[0], fchg)} "
                         f"投信{ic[1]:+,.0f} 自營{ic[2]:+,.0f}")
        if disp == "台指期" and cost:
            c, settle, pos, pnl, cstart = cost
            block.append(f"　外資成本(估) {c:,.0f},結算 {settle:,.0f},"
                         f"{'浮盈' if pnl >= 0 else '浮虧'}{abs(pnl):,.0f}億"
                         f"(自{cstart[5:].replace('-', '/')}累計)")
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
    price_dd = conn.execute(
        "SELECT MAX(data_date) FROM fut_price").fetchone()[0]
    return "\n".join(lines), {"lt": curr, "inst": icurr, "price": price_dd}
