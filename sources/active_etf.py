"""資料源：台股主動式 ETF 每日持股。

對外契約(供 main 調度)：
    NAME                     來源名稱
    fetch(conn)              抓五檔最新持股入庫,回傳失敗清單
    backfill(conn, days)     回補歷史(元大不支援)
    report(conn)            產出每相鄰資料日的 Markdown 買賣報告
    build_message(conn)      → (LINE 訊息文字, 簽章 dict) 供去重

各投信皆為現金申購/買回:申贖進出現金、不直接改變持股,所以股數差就是
經理人實際買賣,不做規模校正;流通單位數變化列在開頭供判讀。
"""
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from core import store

NAME = "active_etf"
REPORT_DIR = Path(__file__).parent.parent / "reports"
TOP_N = 5  # LINE 訊息每檔加碼/減碼各列前幾筆,其餘彙總為一行

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

FUNDS = {
    "00981A": {"issuer": "uni", "uni_code": "49YTW", "name": "主動統一台股增長"},
    "00403A": {"issuer": "uni", "uni_code": "63YTW", "name": "主動統一升級50"},
    "00988A": {"issuer": "uni", "uni_code": "61YTW", "name": "主動統一全球創新"},
    "00991A": {"issuer": "fuhhwa", "page_id": "ETF23", "name": "主動復華未來50"},
    "00990A": {"issuer": "yuanta", "name": "主動元大AI新經濟"},
    "00992A": {"issuer": "capital", "cap_id": 500, "name": "主動群益科技創新"},
    "00982A": {"issuer": "capital", "cap_id": 399, "name": "主動群益台灣強棒"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_day (
    etf          TEXT NOT NULL,
    data_date    TEXT NOT NULL,   -- YYYY-MM-DD,投信揭露的資料日期
    nav          REAL,
    units        REAL,            -- 已發行受益權單位總數
    total_assets REAL,
    fetched_at   TEXT,
    PRIMARY KEY (etf, data_date)
);
CREATE TABLE IF NOT EXISTS holding (
    etf       TEXT NOT NULL,
    data_date TEXT NOT NULL,
    code      TEXT NOT NULL,
    name      TEXT,
    shares    REAL,
    amount    REAL,
    weight    REAL,
    PRIMARY KEY (etf, data_date, code)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


# ================================================================ 抓取
_uni_session = None


def _uni_get_session():
    global _uni_session
    if _uni_session is None:
        s = requests.Session()
        s.headers["User-Agent"] = UA
        # 先逛一次頁面拿 Nexusguard / ASP.NET session cookie
        r = s.get("https://www.ezmoney.com.tw/ETF/Transaction/PCF", timeout=30)
        r.raise_for_status()
        _uni_session = s
    return _uni_session


def _to_roc(d: date) -> str:
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def _uni_date(v: str) -> str:
    """ezmoney 的 TranDate 有兩種格式:ISO 與 ASP.NET 的 /Date(毫秒)/。"""
    if v.startswith("/Date("):
        ms = int(re.search(r"-?\d+", v).group())
        tw = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(ms / 1000, tz=tw).date().isoformat()
    return v[:10]


def _fetch_uni(etf, target=None):
    s = _uni_get_session()
    payload = {
        "fundCode": FUNDS[etf]["uni_code"],
        "date": _to_roc(target or date.today()),
        "specificDate": target is not None,
    }
    r = s.post(
        "https://www.ezmoney.com.tw/ETF/Transaction/GetPCF",
        json=payload,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.ezmoney.com.tw/ETF/Transaction/PCF",
        },
        timeout=30,
    )
    r.raise_for_status()
    if "json" not in r.headers.get("Content-Type", ""):
        raise RuntimeError(f"ezmoney 回應不是 JSON({etf}),網站可能改版或被擋")
    d = r.json()
    pcf = {row["PCFCode"]: row for row in d["pcf"]}
    if all(row["Amount"] == 0 for row in d["pcf"]):
        raise LookupError(f"ezmoney {etf} {payload['date']} 無資料")
    data_date = _uni_date(pcf["NAV"]["TranDate"])
    holdings = []
    for asset in d["asset"]:
        if asset["AssetCode"] != "ST" or not asset["Details"]:
            continue
        for row in asset["Details"]:
            holdings.append({
                "code": row["DetailCode"].strip(),
                "name": row["DetailName"].strip(),
                "shares": row["Share"],
                "amount": row["Amount"],
                "weight": row["NavRate"],
            })
    if not holdings:
        raise LookupError(f"ezmoney {etf} {data_date} 無持股明細")
    return {
        "etf": etf, "data_date": data_date,
        "nav": pcf["P_UNIT"]["Amount"], "units": pcf["OUT_UNIT"]["Amount"],
        "total_assets": pcf["NAV"]["Amount"], "holdings": holdings,
        "raw": r.content, "raw_ext": "json",
    }


def _xlsx_rows(content: bytes):
    z = zipfile.ZipFile(BytesIO(content))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    ss = [el.findtext("m:t", default="", namespaces=ns)
          for el in ET.parse(z.open("xl/sharedStrings.xml")).getroot()]
    sheet = ET.parse(z.open("xl/worksheets/sheet1.xml")).getroot()
    for r in sheet.findall(".//m:row", ns):
        vals = []
        for c in r.findall("m:c", ns):
            v = c.findtext("m:v", default="", namespaces=ns)
            if c.get("t") == "s" and v:
                v = ss[int(v)]
            vals.append(v)
        yield vals


def _num(s) -> float:
    return float(str(s).replace(",", "").replace("%", ""))


def _fetch_fuhhwa(etf, target=None):
    page_id = FUNDS[etf]["page_id"]
    candidates = ([target] if target else
                  [date.today() - timedelta(days=i) for i in range(8)])
    content = None
    for d in candidates:
        url = f"https://www.fhtrust.com.tw/api/assetsExcel/{page_id}/{d:%Y%m%d}"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        if r.status_code == 200 and r.content[:2] == b"PK":
            content = r.content
            break
    if content is None:
        raise LookupError(f"fhtrust {etf} 找不到可用的持股 Excel")
    rows = list(_xlsx_rows(content))
    data_date = nav = units = total_assets = None
    holdings = []
    in_table = False
    for i, row in enumerate(rows):
        cell0 = row[0] if row else ""
        if cell0.startswith("日期"):
            m = re.search(r"(\d{4})/(\d{2})/(\d{2})", cell0)
            data_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        elif cell0 == "基金資產淨值":
            total_assets = _num(rows[i + 1][0])
        elif cell0 == "基金在外流通單位數":
            units = _num(rows[i + 1][0])
        elif cell0 == "基金每單位淨值":
            nav = _num(rows[i + 1][0])
        elif cell0 == "證券代號":
            in_table = True
        elif in_table and len(row) >= 5 and row[0]:
            holdings.append({
                "code": row[0].strip(), "name": row[1].strip(),
                "shares": _num(row[2]), "amount": _num(row[3]),
                "weight": _num(row[4]),
            })
    if not (data_date and holdings):
        raise RuntimeError(f"fhtrust {etf} Excel 格式解析失敗,格式可能改版")
    return {
        "etf": etf, "data_date": data_date, "nav": nav, "units": units,
        "total_assets": total_assets, "holdings": holdings,
        "raw": content, "raw_ext": "xlsx",
    }


def _fetch_yuanta(etf, target=None):
    if target is not None:
        raise ValueError("元大 API 不支援查歷史日期")
    r = requests.get(
        "https://etfapi.yuantaetfs.com/ectranslation/api/bridge",
        params={
            "APIType": "ETFAPI", "FuncId": "PCF/Daily", "ticker": etf,
            "CompanyName": "YUANTAFUNDS", "AppName": "ETF",
            "Device": "3", "Platform": "ETF", "DeviceId": "null",
            "PageName": f"/product/detail/{etf}/ratio",
        },
        headers={"User-Agent": UA, "Accept": "application/json",
                 "Referer": "https://www.yuantaetfs.com/",
                 "Origin": "https://www.yuantaetfs.com"},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    pcf = d["PCF"]
    td = pcf["trandate"]
    total_assets = pcf["totalav"]
    holdings = []
    for row in d["FundWeights"]["StockWeights"]:
        holdings.append({
            "code": row["code"].strip(), "name": row["name"].strip(),
            "shares": row["qty"],
            # 元大不給個股市值,用 淨資產×權重 估算(供排序用)
            "amount": round(total_assets * row["weights"] / 100),
            "weight": row["weights"],
        })
    return {
        "etf": etf, "data_date": f"{td[:4]}-{td[4:6]}-{td[6:8]}",
        "nav": pcf["nav"], "units": pcf["osunit"],
        "total_assets": total_assets, "holdings": holdings,
        "raw": r.content, "raw_ext": "json",
    }


# ---------------------------------------------------------------- 群益投信
# capitalfund.com.tw 在 Imperva 後面,標準 curl/requests 的 TLS 指紋過不了,
# 需用 curl_cffi 模擬 Chrome。資料走 /CFWeb/api/etf/ 的 POST JSON API。
_CAPITAL = "https://www.capitalfund.com.tw"
_CAP_H = {"Accept": "application/json", "Content-Type": "application/json",
          "Origin": _CAPITAL}


def _fetch_capital(etf, target=None):
    from curl_cffi import requests as cr  # 延後匯入:僅群益需要此依賴
    cap_id = FUNDS[etf]["cap_id"]
    if target is not None:
        date_str = f"{target:%Y/%m/%d}"
    else:
        nav = cr.post(f"{_CAPITAL}/CFWeb/api/etf/nav", json={"id": 0},
                      headers=_CAP_H, impersonate="chrome", timeout=30
                      ).json().get("data", [])
        date_str = next((x.get("date1") for x in nav
                         if str(x.get("fundId")) == str(cap_id)), None)
        if not date_str:
            raise LookupError(f"capital {etf} 取不到最新日期")
    r = cr.post(f"{_CAPITAL}/CFWeb/api/etf/buyback",
                json={"fundId": cap_id, "date": date_str},
                headers=_CAP_H, impersonate="chrome", timeout=30)
    r.raise_for_status()
    d = r.json().get("data") or {}
    pcf, stocks = d.get("pcf") or {}, d.get("stocks") or []
    if not stocks:
        raise LookupError(f"capital {etf} {date_str} 無持股明細")
    total_assets = pcf["nav"]  # 群益 pcf.nav 是基金淨資產(元),pUnit 才是每單位淨值
    holdings = [{
        "code": str(s["stocNo"]).strip(), "name": s["stocName"].strip(),
        "shares": s["share"],
        # 群益不給個股市值,用 淨資產×權重 估算(供排序用)
        "amount": round(total_assets * s["weight"] / 100),
        "weight": s["weight"],
    } for s in stocks]
    return {
        "etf": etf, "data_date": pcf["date1"],
        "nav": pcf["pUnit"], "units": pcf["totUnit"],
        "total_assets": total_assets, "holdings": holdings,
        "raw": r.content, "raw_ext": "json",
    }


_FETCHERS = {"uni": _fetch_uni, "fuhhwa": _fetch_fuhhwa,
             "yuanta": _fetch_yuanta, "capital": _fetch_capital}


def _fetch_one(etf, target=None):
    return _FETCHERS[FUNDS[etf]["issuer"]](etf, target)


def _save(conn, snap):
    store.save_raw(NAME, snap["data_date"], snap["etf"],
                   snap["raw_ext"], snap["raw"])
    etf, dd = snap["etf"], snap["data_date"]
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO fund_day VALUES (?,?,?,?,?,?)",
            (etf, dd, snap["nav"], snap["units"], snap["total_assets"],
             datetime.now().isoformat(timespec="seconds")))
        conn.execute("DELETE FROM holding WHERE etf=? AND data_date=?", (etf, dd))
        conn.executemany(
            "INSERT INTO holding VALUES (?,?,?,?,?,?,?)",
            [(etf, dd, h["code"], h["name"], h["shares"], h["amount"],
              h["weight"]) for h in snap["holdings"]])


def fetch(conn):
    init(conn)
    failures = []
    for etf in FUNDS:
        try:
            snap = _fetch_one(etf)
            _save(conn, snap)
            print(f"[fetch] {etf}: 資料日 {snap['data_date']},"
                  f"{len(snap['holdings'])} 檔持股")
        except Exception as e:
            failures.append(etf)
            print(f"[fetch] {etf}: 失敗 — {e}", file=sys.stderr)
        time.sleep(1)
    return failures


def backfill(conn, days):
    init(conn)
    for etf in FUNDS:
        # 元大 API 只有最新一日;群益歷史日期行為未驗證,暫只抓最新往前累積
        if FUNDS[etf]["issuer"] in ("yuanta", "capital"):
            print(f"[backfill] {etf}: 來源不回補歷史,略過")
            continue
        for i in range(1, days + 1):
            d = date.today() - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            try:
                # 統一的 date 是公告日,回應資料日通常為前一交易日;
                # 一律以回應內的實際資料日入庫(重複日期冪等覆蓋)。
                snap = _fetch_one(etf, d)
                _save(conn, snap)
                print(f"[backfill] {etf} 查 {d} → 資料日 {snap['data_date']},"
                      f"{len(snap['holdings'])} 檔持股")
            except LookupError:
                print(f"[backfill] {etf} {d}: 無資料(休市或未揭露)")
            except Exception as e:
                print(f"[backfill] {etf} {d}: 失敗 — {e}", file=sys.stderr)
            time.sleep(1)


# ================================================================ 差異計算
def _fund_day(conn, etf, dd):
    row = conn.execute(
        "SELECT nav, units, total_assets FROM fund_day "
        "WHERE etf=? AND data_date=?", (etf, dd)).fetchone()
    return {"nav": row[0], "units": row[1], "total_assets": row[2]}


def _holdings(conn, etf, dd):
    rows = conn.execute(
        "SELECT code, name, shares, amount, weight FROM holding "
        "WHERE etf=? AND data_date=?", (etf, dd)).fetchall()
    return {r[0]: {"name": r[1], "shares": r[2], "amount": r[3],
                   "weight": r[4]} for r in rows}


def _recent_dates(conn, etf, n):
    return [r[0] for r in conn.execute(
        "SELECT data_date FROM fund_day WHERE etf=? "
        "ORDER BY data_date DESC LIMIT ?", (etf, n))]


# ================================================================ Markdown 報告
def _diff_report(conn, etf, prev_d, curr_d):
    curr, prev = _holdings(conn, etf, curr_d), _holdings(conn, etf, prev_d)
    fc, fp = _fund_day(conn, etf, curr_d), _fund_day(conn, etf, prev_d)

    def price(code):
        h = curr.get(code) or prev.get(code)
        return h["amount"] / h["shares"] if h["amount"] and h["shares"] else None

    new, gone, inc, dec = [], [], [], []
    for code in sorted(set(curr) | set(prev)):
        c, p = curr.get(code), prev.get(code)
        if p is None:
            new.append((code, c["name"], c["shares"], c["amount"]))
        elif c is None:
            gone.append((code, p["name"], p["shares"],
                         (price(code) or 0) * p["shares"]))
        elif c["shares"] != p["shares"]:
            delta = c["shares"] - p["shares"]
            est = (price(code) or 0) * delta
            item = (code, c["name"], p["shares"], c["shares"], delta, est,
                    c["weight"] - p["weight"])
            (inc if delta > 0 else dec).append(item)
    inc.sort(key=lambda x: -abs(x[5]))
    dec.sort(key=lambda x: -abs(x[5]))
    new.sort(key=lambda x: -(x[3] or 0))
    gone.sort(key=lambda x: -(x[3] or 0))

    du = ((fc["units"] - fp["units"]) / fp["units"] * 100
          if fc["units"] and fp["units"] else 0)
    L = [f"# {etf} 持股變化：{prev_d} → {curr_d}", ""]
    L.append(f"- 流通單位數：{fp['units']:,.0f} → {fc['units']:,.0f}"
             f"（{du:+.2f}%，現金申贖，不直接反映在持股）")
    L.append(f"- 淨值：{fp['nav']} → {fc['nav']}")
    L.append(f"- 持股檔數：{len(prev)} → {len(curr)}")
    L.append("")

    def money(v):
        return f"{v/1e8:,.2f} 億" if abs(v) >= 1e8 else f"{v/1e4:,.0f} 萬"

    if new:
        L += ["## 新進場", "", "| 股票 | 股數 | 市值 |", "|---|---:|---:|"]
        L += [f"| {c} {n} | {s:,.0f} | {money(a) if a else '—'} |"
              for c, n, s, a in new] + [""]
    if gone:
        L += ["## 清倉", "", "| 股票 | 原股數 | 估計市值 |", "|---|---:|---:|"]
        L += [f"| {c} {n} | {s:,.0f} | {money(a) if a else '—'} |"
              for c, n, s, a in gone] + [""]
    for title, items in (("加碼", inc), ("減碼", dec)):
        if items:
            L += [f"## {title}", "",
                  "| 股票 | 前日股數 | 今日股數 | 變化 | 估計金額 | 權重變化 |",
                  "|---|---:|---:|---:|---:|---:|"]
            L += [f"| {c} {n} | {ps:,.0f} | {cs:,.0f} | {d:+,.0f} "
                  f"| {money(est) if est else '—'} | {dw:+.2f}% |"
                  for c, n, ps, cs, d, est, dw in items] + [""]
    if not (new or gone or inc or dec):
        L += ["（持股無任何變化）", ""]
    return "\n".join(L)


def report(conn):
    written = []
    for etf in FUNDS:
        dates = [r[0] for r in conn.execute(
            "SELECT data_date FROM fund_day WHERE etf=? ORDER BY data_date",
            (etf,))]
        if len(dates) < 2:
            print(f"[report] {etf}: 資料不足兩天,略過")
            continue
        for prev_d, curr_d in zip(dates, dates[1:]):
            out = REPORT_DIR / curr_d
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{etf}.md").write_text(
                _diff_report(conn, etf, prev_d, curr_d), encoding="utf-8")
            written.append(out / f"{etf}.md")
        print(f"[report] {etf}: {dates[0]} ～ {dates[-1]} "
              f"共 {len(dates) - 1} 份報告")
    return written


# ================================================================ LINE 摘要
def _money(v):
    return f"{v/1e8:,.1f}億" if abs(v) >= 1e8 else f"{v/1e4:,.0f}萬"


def _fund_summary(conn, etf):
    dates = _recent_dates(conn, etf, 2)
    if len(dates) < 2:
        return (None, dates[0] if dates else None)
    curr_d, prev_d = dates
    curr, prev = _holdings(conn, etf, curr_d), _holdings(conn, etf, prev_d)
    fc, fp = _fund_day(conn, etf, curr_d), _fund_day(conn, etf, prev_d)

    def price(code):
        h = curr.get(code) or prev.get(code)
        return h["amount"] / h["shares"] if h["amount"] and h["shares"] else 0

    new, gone, inc, dec = [], [], [], []
    for code in set(curr) | set(prev):
        c, p = curr.get(code), prev.get(code)
        if p is None:
            new.append((c["amount"] or 0, f"🆕 {c['name']} {c['shares']:,.0f}股"
                        f"({_money(c['amount'])})" if c["amount"] else
                        f"🆕 {c['name']} {c['shares']:,.0f}股"))
        elif c is None:
            est = price(code) * p["shares"]
            gone.append((est, f"❌ 清倉 {p['name']}({_money(est)})"))
        elif c["shares"] != p["shares"]:
            d = c["shares"] - p["shares"]
            est = price(code) * d
            (inc if d > 0 else dec).append(
                (abs(est), f"{c['name']} {d:+,.0f}股({_money(est)})"))

    du = ((fc["units"] - fp["units"]) / fp["units"] * 100
          if fc["units"] and fp["units"] else 0)
    head = (f"▍{etf} {FUNDS[etf]['name']}（{prev_d[5:]}→{curr_d[5:]}，"
            f"申贖{du:+.1f}%）")
    if not (new or gone or inc or dec):
        return [head, "持股無變動"], curr_d
    lines = [head]
    lines += [t for _, t in sorted(new, key=lambda x: -x[0])]
    lines += [t for _, t in sorted(gone, key=lambda x: -x[0])]
    for label, items in (("➕ 加碼", inc), ("➖ 減碼", dec)):
        if not items:
            continue
        items.sort(key=lambda x: -x[0])
        lines.append(label)
        lines += [f"　{t}" for _, t in items[:TOP_N]]
        if len(items) > TOP_N:
            lines.append(f"　…另 {len(items) - TOP_N} 筆")
    return lines, curr_d


def build_message(conn):
    """回傳 (LINE 訊息文字, 簽章 dict)。簽章為各檔最新資料日,供去重。"""
    blocks, sig = [], {}
    for etf in FUNDS:
        lines, curr_d = _fund_summary(conn, etf)
        if curr_d:
            sig[etf] = curr_d
        if lines:
            blocks.append("\n".join(lines))
    if not blocks:
        return None, sig
    text = f"📊 主動式ETF持股異動 {date.today():%m/%d}\n\n" + "\n\n".join(blocks)
    return text, sig
