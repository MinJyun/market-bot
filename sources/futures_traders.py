"""資料源:期交所期貨大額交易人未沖銷部位(五大/十大交易人,含特定法人)。

追蹤股價指數期貨「所有契約」列:前五大/前十大交易人(及其中特定法人)的
買方、賣方部位,推算特定法人淨部位(買方-賣方,正=淨多、負=淨空)。
資料來自期交所每日揭露頁 largeTraderFutQry;TAIFEX 有 bot 防護,用 curl_cffi
模擬 Chrome 才抓得到。每格數字為「全部交易人(其中特定法人)」。

對外契約:NAME / fetch(conn) / build_message(conn)。

不做 backfill:期交所僅「當日預設頁」提供帶語意 headers、可穩定解析的表格,
指定歷史日期會回另一種凌亂版面;故歷史從今天起每日往前累積,第一次的
「前日變化」會在第二次每日執行後出現。
"""
import re
from datetime import datetime

from core import store

NAME = "futures_traders"
QRY = "https://www.taifex.com.tw/cht/3/largeTraderFutQry"

# 頁面契約名稱前綴 → 顯示名(台指期為 TX+MTX/4+TMF/20 合併計)
CONTRACTS = {
    "臺股期貨": "台指期",
    "電子期貨": "電子期",
    "金融期貨": "金融期",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS futures_lt (
    contract   TEXT NOT NULL,
    data_date  TEXT NOT NULL,       -- YYYY-MM-DD
    buy5   REAL, buy10  REAL, sell5  REAL, sell10  REAL,        -- 特定法人部位
    buy5_all REAL, buy10_all REAL, sell5_all REAL, sell10_all REAL,  -- 全部交易人
    oi     REAL,                    -- 全市場未沖銷部位數
    fetched_at TEXT,
    PRIMARY KEY (contract, data_date)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _num(s):
    s = re.sub(r"[^\d\-]", "", s or "")
    return float(s) if s not in ("", "-") else 0.0


def _cell(td):
    """'74,926<br>(74,926)' → (全部交易人, 特定法人)。"""
    nums = re.findall(r"-?[\d,]+", re.sub(r"<[^>]+>", "\n", td))
    allv = _num(nums[0]) if nums else 0.0
    spv = _num(nums[1]) if len(nums) > 1 else 0.0
    return allv, spv


def _parse_contract(html, name):
    """回傳該契約「所有契約」列的部位 dict,找不到回 None。"""
    # 錨定到資料表的 name_a 格子(避免命中導覽選單的同名文字)
    m = re.search(r'headers="name_a"[^>]*>\s*<div[^>]*>\s*' + re.escape(name),
                  html)
    if not m:
        return None
    tstart = html.rfind("<tr", 0, m.start())
    rs = re.search(r'rowspan="(\d+)"', html[tstart:tstart + 120])
    n = int(rs.group(1)) if rs else 1
    for tr in re.split(r"<tr[ >]", html[tstart:])[1:n + 1]:
        exp = re.search(r'headers="expiry_a"[^>]*>(.*?)</td>', tr, re.S)
        exptxt = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", exp.group(1))) if exp else ""
        if "所有" not in exptxt:
            continue

        def grab(h):
            m = re.search(r'headers="[^"]*' + h + r'[^"]*"[^>]*>(.*?)</td>',
                          tr, re.S)
            return _cell(m.group(1)) if m else (0.0, 0.0)

        b5a, b5 = grab("buyer_a_01_01")
        b10a, b10 = grab("buyer_a_02_01")
        s5a, s5 = grab("seller_a_01_01")
        s10a, s10 = grab("seller_a_02_01")
        oi_m = re.search(r'headers="position_a"[^>]*>(.*?)</td>', tr, re.S)
        oi = _num(re.sub(r"<[^>]+>", "", oi_m.group(1))) if oi_m else 0.0
        return dict(buy5=b5, buy10=b10, sell5=s5, sell10=s10, buy5_all=b5a,
                    buy10_all=b10a, sell5_all=s5a, sell10_all=s10a, oi=oi)
    return None


def _page_date(html):
    m = re.search(r'name="queryDate"[^>]*value="(\d{4})/(\d{2})/(\d{2})"', html)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _fetch_html():
    from curl_cffi import requests as cr  # TAIFEX bot 防護,需模擬 Chrome
    r = cr.get(QRY, impersonate="chrome", timeout=30)  # 預設頁即最新交易日
    r.raise_for_status()
    return r.text


def _store(conn, dd, html, rows):
    store.save_raw(NAME, dd, "largeTraderFut", "html", html.encode("utf-8"))
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        for disp, d in rows.items():
            conn.execute(
                "INSERT OR REPLACE INTO futures_lt VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?)",
                (disp, dd, d["buy5"], d["buy10"], d["sell5"], d["sell10"],
                 d["buy5_all"], d["buy10_all"], d["sell5_all"], d["sell10_all"],
                 d["oi"], now))


def _grab_all(html):
    return {disp: d for name, disp in CONTRACTS.items()
            if (d := _parse_contract(html, name))}


def fetch(conn):
    init(conn)
    try:
        html = _fetch_html()
        dd, rows = _page_date(html), _grab_all(html)
        if not dd or not rows:
            print("[fetch] futures_traders: 頁面解析失敗,期交所可能改版")
            return ["futures_traders"]
        _store(conn, dd, html, rows)
        print(f"[fetch] futures_traders: 資料日 {dd},{len(rows)} 契約")
        return []
    except Exception as e:
        print(f"[fetch] futures_traders: 失敗 — {e}")
        return ["futures_traders"]


def build_message(conn):
    init(conn)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM futures_lt ORDER BY data_date DESC LIMIT 2")]
    if not dates:
        return None, {}
    curr = dates[0]
    prev = dates[1] if len(dates) > 1 else None

    def nets(dd, contract):
        r = conn.execute(
            "SELECT buy5,buy10,sell5,sell10,oi FROM futures_lt "
            "WHERE contract=? AND data_date=?", (contract, dd)).fetchone()
        if not r:
            return None
        return {"n5": r[0] - r[2], "n10": r[1] - r[3], "oi": r[4]}

    def fmt(net, chg):
        s = f"{'淨多' if net >= 0 else '淨空'}{abs(net):,.0f}口"
        if chg is not None:
            s += f"（前日{chg:+,.0f}）"
        return s

    lines = [f"📊 期貨大額交易人·特定法人 {curr[5:].replace('-', '/')}",
             "(所有契約·前五大/前十大交易人淨部位)"]
    for disp in CONTRACTS.values():
        c = nets(curr, disp)
        if not c:
            continue
        p = nets(prev, disp) if prev else None
        lines.append(f"▍{disp} 未平倉 {c['oi']:,.0f} 口")
        lines.append(f"　前五大 {fmt(c['n5'], c['n5'] - p['n5'] if p else None)}")
        lines.append(f"　前十大 {fmt(c['n10'], c['n10'] - p['n10'] if p else None)}")
    return "\n".join(lines), {"date": curr}
