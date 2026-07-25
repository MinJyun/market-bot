"""資料源:期交所臺指選擇權大額交易人未沖銷部位(五大/十大,含特定法人)。

追蹤臺指選擇權買權(Call)、賣權(Put)的「所有契約」列:前五大/前十大交易人
(及其中特定法人)的買方、賣方部位。選擇權 Call/Put 方向意義相反(買 Call 偏
多、買 Put 偏空/避險),故不併成單一淨部位,分別呈現大戶在兩邊的買賣口數。

資料同期貨大額交易人頁,curl_cffi 破 TAIFEX bot 防護。每格為「全部(特定法人)」。
不做 backfill(理由同 futures_traders),歷史每日往前累積。
對外契約:NAME / fetch(conn) / build_message(conn)。
"""
import re
from datetime import datetime

from core import store

NAME = "options_traders"
QRY = "https://www.taifex.com.tw/cht/3/largeTraderOptQry"

# name_a 格子純文字(去空白)→ 顯示名
CONTRACTS = {
    "臺指買權": "臺指Call",
    "臺指賣權": "臺指Put",
}

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
"""


def init(conn):
    conn.executescript(SCHEMA)


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


def fetch(conn):
    init(conn)
    try:
        from curl_cffi import requests as cr
        html = cr.get(QRY, impersonate="chrome", timeout=30).text
        dd = _page_date(html)
        rows = {disp: d for name, disp in CONTRACTS.items()
                if (d := _parse_contract(html, name))}
        if not dd or not rows:
            print("[fetch] options_traders: 頁面解析失敗,期交所可能改版")
            return ["options_traders"]
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
        print(f"[fetch] options_traders: 資料日 {dd},{len(rows)} 契約")
        return []
    except Exception as e:
        print(f"[fetch] options_traders: 失敗 — {e}")
        return ["options_traders"]


def build_message(conn):
    init(conn)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM options_lt ORDER BY data_date DESC LIMIT 2")]
    if not dates:
        return None, {}
    curr = dates[0]
    prev = dates[1] if len(dates) > 1 else None

    def row(dd, contract):
        r = conn.execute(
            "SELECT buy5,buy10,sell5,sell10,oi FROM options_lt "
            "WHERE contract=? AND data_date=?", (contract, dd)).fetchone()
        return r  # (買5,買10,賣5,賣10,oi) 特定法人 or None

    def chg(cur, contract, idx):
        if not prev:
            return ""
        p = row(prev, contract)
        return f"（{cur - p[idx]:+,.0f}）" if p else ""

    lines = [f"📊 臺指選擇權大額交易人·特定法人 {curr[5:].replace('-', '/')}",
             "(所有契約·前五大/前十大交易人)"]
    for disp in CONTRACTS.values():
        r = row(curr, disp)
        if not r:
            continue
        b5, b10, s5, s10, oi = r
        lines.append(f"▍{disp} 未平倉 {oi:,.0f} 口")
        lines.append(f"　前五大 買{b5:,.0f} 賣{s5:,.0f}")
        lines.append(f"　前十大 買{b10:,.0f}{chg(b10, disp, 1)} "
                     f"賣{s10:,.0f}{chg(s10, disp, 3)}")
    return "\n".join(lines), {"date": curr}
