"""資料源:期交所臺指選擇權的機構籌碼(整合兩張表)。

追蹤臺指選擇權,一則訊息同時呈現:
  1. 三大法人(外資/投信/自營)未平倉「多空淨額」—— 臺指選擇權整體,不分買賣權
     方向(該頁未依 Call/Put 拆分,與期交所公開統計口徑一致)。
  2. 買權(Call)、賣權(Put)「所有契約」列的前五大/前十大交易人(特定法人)
     買方、賣方部位。Call/Put 方向意義相反(買 Call 偏多、買 Put 偏空/避險),
     故不併成單一淨部位,分別呈現大戶在兩邊的買賣口數。

資料皆來自期交所,curl_cffi 破 bot 防護。不做 backfill(見下),歷史每日累積。
對外契約:NAME / fetch(conn) / build_message(conn)。

不做 backfill:大額交易人僅當日預設頁提供帶語意 headers、可穩定解析的表格,
指定歷史日期會回另一種凌亂版面;故歷史從今天起每日往前累積。
"""
import re
from datetime import datetime

from core import store

NAME = "options_traders"
LT_URL = "https://www.taifex.com.tw/cht/3/largeTraderOptQry"      # 大額交易人
INST_URL = "https://www.taifex.com.tw/cht/3/optContractsDateExcel"  # 三大法人

# name_a 格子純文字(去空白)→ 顯示名
CONTRACTS = {
    "臺指買權": "臺指Call",
    "臺指賣權": "臺指Put",
}
INST_CONTRACT = "臺指選擇權"  # 三大法人頁的商品名稱,不分 Call/Put

SCHEMA = """
CREATE TABLE IF NOT EXISTS options_lt (
    contract   TEXT NOT NULL,
    data_date  TEXT NOT NULL,
    buy5   REAL, buy10  REAL, sell5  REAL, sell10  REAL,        -- 特定法人
    buy5_all REAL, buy10_all REAL, sell5_all REAL, sell10_all REAL,  -- 全部交易人
    oi     REAL,
    fetched_at TEXT,
    PRIMARY KEY (contract, data_date)
);
CREATE TABLE IF NOT EXISTS options_inst (
    contract   TEXT NOT NULL,
    data_date  TEXT NOT NULL,
    foreign_net REAL, trust_net REAL, dealer_net REAL,          -- 未平倉多空淨額(口)
    fetched_at TEXT,
    PRIMARY KEY (contract, data_date)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _int(s):
    s = re.sub(r"[^\d\-]", "", s or "")
    return int(s) if s not in ("", "-") else 0


# ================================================================ 大額交易人
def _cell(td):
    """'7,990<br>(666)' → (全部交易人, 特定法人)。"""
    nums = re.findall(r"-?[\d,]+", re.sub(r"<[^>]+>", "\n", td))
    to = lambda x: float(re.sub(r"[^\d\-]", "", x) or 0)
    return (to(nums[0]) if nums else 0.0,
            to(nums[1]) if len(nums) > 1 else 0.0)


def _parse_contract(html, name):
    """走訪 name_a 格子,依純文字比對契約,回傳「所有契約」列部位 dict。"""
    for m in re.finditer(r'headers="name_a"', html):
        txt = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", html[m.end():m.end() + 140]))
        if name not in txt:
            continue
        tstart = html.rfind("<tr", 0, m.start())
        rs = re.search(r'rowspan="(\d+)"', html[tstart:m.start() + 5])
        n = int(rs.group(1)) if rs else 1
        for tr in re.split(r"<tr[ >]", html[tstart:])[1:n + 1]:
            exp = re.search(r'headers="expiry_a"[^>]*>(.*?)</td>', tr, re.S)
            if not exp or "所有" not in re.sub(r"\s|<[^>]+>", "", exp.group(1)):
                continue

            def grab(h):
                mm = re.search(r'headers="[^"]*' + h + r'[^"]*"[^>]*>(.*?)</td>',
                               tr, re.S)
                return _cell(mm.group(1)) if mm else (0.0, 0.0)

            b5a, b5 = grab("buyer_a_01_01")
            b10a, b10 = grab("buyer_a_02_01")
            s5a, s5 = grab("seller_a_01_01")
            s10a, s10 = grab("seller_a_02_01")
            oi_m = re.search(r'headers="position_a"[^>]*>(.*?)</td>', tr, re.S)
            oi = _cell(oi_m.group(1))[0] if oi_m else 0.0
            return dict(buy5=b5, buy10=b10, sell5=s5, sell10=s10, buy5_all=b5a,
                        buy10_all=b10a, sell5_all=s5a, sell10_all=s10a, oi=oi)
    return None


def _page_date(html):
    m = re.search(r'name="queryDate"[^>]*value="(\d{4})/(\d{2})/(\d{2})"', html)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


# ================================================================ 三大法人
def _parse_inst(html):
    """回傳 {身份別: 淨口}(僅取 INST_CONTRACT 那列)。

    表格式同 futures_traders 的三大法人頁:大寫 <TR>/<TD>,每商品三列
    (自營商/投信/外資),商品名稱用 rowspan;取「未平倉餘額-多空淨額-口數」
    (身份別後第 11 個數字欄)。
    """
    def cells(tr):
        return [re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", c))
                for c in re.split(r"(?i)<t[dh][^>]*>", tr)[1:]]

    out, contract = {}, None
    for tr in re.split(r"(?i)<tr[^>]*>", html)[1:]:
        cs = [c for c in cells(tr) if c != ""]
        if len(cs) >= 15 and re.match(r"^\d+$", cs[0]) and cs[2] in (
                "自營商", "投信", "外資"):
            contract, who, nums = cs[1], cs[2], cs[3:]
        elif len(cs) >= 13 and cs[0] in ("自營商", "投信", "外資"):
            who, nums = cs[0], cs[1:]
        else:
            continue
        if contract == INST_CONTRACT:
            out[who] = _int(nums[10])
    return out


def _inst_date(html):
    m = re.search(r"日期\s*(\d{4})/(\d{2})/(\d{2})", html)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


# ================================================================ 抓取
def fetch(conn):
    init(conn)
    from curl_cffi import requests as cr
    fails = []
    # 1) 大額交易人
    try:
        html = cr.get(LT_URL, impersonate="chrome", timeout=30).text
        dd = _page_date(html)
        rows = {disp: d for name, disp in CONTRACTS.items()
                if (d := _parse_contract(html, name))}
        if not dd or not rows:
            raise RuntimeError("大額交易人頁解析失敗")
        store.save_raw(NAME, dd, "largeTraderOpt", "html", html.encode("utf-8"))
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            for disp, d in rows.items():
                conn.execute(
                    "INSERT OR REPLACE INTO options_lt VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (disp, dd, d["buy5"], d["buy10"], d["sell5"], d["sell10"],
                     d["buy5_all"], d["buy10_all"], d["sell5_all"],
                     d["sell10_all"], d["oi"], now))
        print(f"[fetch] options_traders 大額: 資料日 {dd},{len(rows)} 契約")
    except Exception as e:
        fails.append("options_lt")
        print(f"[fetch] options_traders 大額: 失敗 — {e}")
    # 2) 三大法人
    try:
        html = cr.get(INST_URL, impersonate="chrome", timeout=30).text
        dd = _inst_date(html)
        inst = _parse_inst(html)
        if not dd or not inst:
            raise RuntimeError("三大法人頁解析失敗")
        store.save_raw(NAME, dd, "optContracts", "html", html.encode("utf-8"))
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO options_inst VALUES (?,?,?,?,?,?)",
                (INST_CONTRACT, dd, inst.get("外資", 0), inst.get("投信", 0),
                 inst.get("自營商", 0), now))
        print(f"[fetch] options_traders 三大法人: 資料日 {dd}")
    except Exception as e:
        fails.append("options_inst")
        print(f"[fetch] options_traders 三大法人: 失敗 — {e}")
    return ["options_traders"] if len(fails) == 2 else []


# ================================================================ LINE 訊息
def build_message(conn):
    init(conn)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM options_lt ORDER BY data_date DESC LIMIT 2")]
    idates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM options_inst ORDER BY data_date DESC LIMIT 2")]
    if not dates and not idates:
        return None, {}
    curr = dates[0] if dates else idates[0]
    prev = dates[1] if len(dates) > 1 else None
    icurr = idates[0] if idates else None
    iprev = idates[1] if len(idates) > 1 else None

    def row(dd, contract):
        r = conn.execute(
            "SELECT buy5,buy10,sell5,sell10,oi FROM options_lt "
            "WHERE contract=? AND data_date=?", (contract, dd)).fetchone()
        return r  # (買5,買10,賣5,賣10,oi) 特定法人 or None

    def inst(dd):
        return conn.execute(
            "SELECT foreign_net,trust_net,dealer_net FROM options_inst "
            "WHERE contract=? AND data_date=?", (INST_CONTRACT, dd)).fetchone()

    def chg(cur, contract, idx):
        if not prev:
            return ""
        p = row(prev, contract)
        return f"（{cur - p[idx]:+,.0f}）" if p else ""

    lines = [f"📊 臺指選擇權機構籌碼 {curr[5:].replace('-', '/')}"]
    ic = inst(icurr) if icurr else None
    if ic:
        ip = inst(iprev) if iprev else None
        fchg = f"（前日{ic[0] - ip[0]:+,.0f}）" if ip else ""
        lines.append(f"三大法人淨(不分買賣權):外資{ic[0]:+,.0f}{fchg} "
                     f"投信{ic[1]:+,.0f} 自營{ic[2]:+,.0f}")
    lines.append("(所有契約·前五大/前十大特定法人)")
    for disp in CONTRACTS.values():
        r = row(curr, disp)
        if not r:
            continue
        b5, b10, s5, s10, oi = r
        lines.append(f"▍{disp} 未平倉 {oi:,.0f} 口")
        lines.append(f"　前五大 買{b5:,.0f} 賣{s5:,.0f}")
        lines.append(f"　前十大 買{b10:,.0f}{chg(b10, disp, 1)} "
                     f"賣{s10:,.0f}{chg(s10, disp, 3)}")
    return "\n".join(lines), {"date": curr, "inst": icurr}
