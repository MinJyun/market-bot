"""資料源:CME FedWatch 式的 FOMC 升降息機率。

CME 官網禁止程式抓取(打 API 直接回 IP blocked),所以自行從 30 天聯邦資金
期貨(ZQ)反推 — 與 FedWatch 同源同法,已對帳到 0.05pp 內(見下)。

原理:ZQ 合約結算於該月每日 EFFR 的算術平均。無 FOMC 會議的月份,整月
利率恆定,合約隱含利率直接等於當期 EFFR;有會議的月份則是「會前 × 天數
+ 會後 × 天數」的加權平均,可反推會後利率。逐次會議解出預期 EFFR 後,
單次變動幅度 ÷ 一碼(0.25%)即該次會議的升息機率,再遞迴卷積成各目標
區間的條件機率分布(即 FedWatch 表格那張圖)。

關鍵細節:同一個未知數常有多個月份的方程可解,必須取「該未知數權重最大」
的那個 — 會後段天數占比越大,報價誤差被放大得越少。用會議當月合約反推
會差到 1.3pp,改用下一個無會議月份(權重 1.0)才對得上 CME。

對帳:2026-08-29 以 CME 截圖報價重算 41 格條件機率,最大誤差 0.05pp、
平均 0.026pp(純四捨五入),2026/9/16 會議 57.0% 完全吻合。

資料來源(皆免金鑰):
- ZQ 各月報價:Yahoo Finance chart API(同 macro.py 的抓法)
- 現行 EFFR 與目標區間:紐約聯準銀行 markets API
- FOMC 會議日曆:federalreserve.gov

fetch 即為當日快照,不支援 backfill(期貨歷史價可回補,但 CME 用即時價,
補出來的數與當時官網不一致,寧可從今天起逐日累積)。
對外契約:NAME / fetch(conn) / build_message(conn)。
"""
import calendar
import datetime as dt
import re

import requests

from core import store

NAME = "fedwatch"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
EFFR_URL = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"}

CODE = "FGHJKMNQUVXZ"   # 期貨月份代碼 F=1月 … Z=12月
STEP = 0.25             # 一碼
MONTHS = 20             # 往後抓幾個月的合約
MON = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fedwatch (
    data_date    TEXT NOT NULL,     -- 快照日(ZQ 報價所屬美東交易日)
    meeting_date TEXT NOT NULL,     -- FOMC 決議日
    bucket_bp    INTEGER NOT NULL,  -- 目標區間下緣(bps),如 375 = 3.75%
    prob         REAL,              -- 該區間機率 0~1
    PRIMARY KEY (data_date, meeting_date, bucket_bp)
);
CREATE TABLE IF NOT EXISTS fedwatch_rate (
    data_date    TEXT NOT NULL,
    meeting_date TEXT NOT NULL,     -- 'current' 表示會議前的現行水準
    exp_effr     REAL,              -- 該次會議後的預期 EFFR(%)
    PRIMARY KEY (data_date, meeting_date)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


# ------------------------------------------------------------------ 外部資料
def _fetch_effr():
    """現行 EFFR 與目標區間下緣(bps)。"""
    r = requests.get(EFFR_URL, headers=UA, timeout=20)
    r.raise_for_status()
    d = r.json()["refRates"][0]
    store.save_raw(NAME, d["effectiveDate"], "effr", "json", r.content)
    return d["percentRate"], int(round(d["targetRateFrom"] * 100))


def _fetch_calendar():
    """未來的 FOMC 決議日(會期最後一天)。"""
    r = requests.get(FOMC_URL, headers=UA, timeout=25)
    r.raise_for_status()
    today, out = dt.date.today(), []
    for ym in re.finditer(r"(\d{4}) FOMC Meetings(.*?)(?=\d{4} FOMC Meetings|$)",
                          r.text, re.S):
        year = int(ym.group(1))
        for mon, days in re.findall(
                r"fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Za-z]+)"
                r".*?fomc-meeting__date[^>]*>\s*(?:<strong>)?\s*([\d\-–]+)",
                ym.group(2), re.S):
            if mon not in MON:
                continue
            day = int(re.split(r"[-–]", days)[-1])   # 決議日 = 最後一天
            d = dt.date(year, MON[mon], day)
            if d >= today:
                out.append(d)
    if not out:
        raise RuntimeError("FOMC 日曆解析失敗,聯準會可能改版")
    store.save_raw(NAME, today.isoformat(), "fomc_calendar", "html",
                   r.text.encode("utf-8"))
    return sorted(out)


def _fetch_futures():
    """各月 ZQ 最新價。回傳 ({(年,月): 價}, 快照日)。

    用 regularMarketPrice 而非日線收盤 — CME FedWatch 走即時價,兩者在
    盤中可差 2~3bp(足以讓機率差 1pp 以上)。早上 8 點排程跑時美股已收盤,
    取到的即是當日收盤價。
    """
    px, snap = {}, None
    d = dt.date.today().replace(day=1)
    for _ in range(MONTHS):
        sym = f"ZQ{CODE[d.month - 1]}{d.year % 100}.CBT"
        try:
            r = requests.get(YAHOO.format(sym),
                             params={"range": "1d", "interval": "1d"},
                             headers=UA, timeout=20)
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            p = meta.get("regularMarketPrice")
            if p:
                px[(d.year, d.month)] = p
                t = meta.get("regularMarketTime")
                if t:   # 期貨報價時間為美東,轉當地日期作為快照日
                    snap = max(snap or "", dt.datetime.fromtimestamp(t).date()
                               .isoformat())
        except Exception:
            pass        # 遠月合約流動性低、偶爾缺報價,略過不算失敗
        d = (d.replace(day=28) + dt.timedelta(days=7)).replace(day=1)
    if len(px) < 6:
        raise RuntimeError(f"ZQ 報價不足({len(px)} 個月),無法解算")
    return px, (snap or dt.date.today().isoformat())


# -------------------------------------------------------------------- 解算
def _month_weights(y, m, meets):
    """該月各利率段的天數占比。段 k = 當日前已生效的會議數(決議次日生效)。"""
    n = calendar.monthrange(y, m)[1]
    w = {}
    for day in range(1, n + 1):
        k = sum(1 for mt in meets if dt.date(y, m, day) > mt)
        w[k] = w.get(k, 0) + 1 / n
    return w


def _solve(px, meets, effr):
    """解出每次會議後的預期 EFFR。r[0] 為現行水準。

    多個方程能解同一未知數時取該未知數權重最大者:會後段占比越大,報價
    誤差被 1/w 放大得越少。無會議月份權重為 1.0,自然成為最優錨點。
    """
    r = {0: effr}
    w_all = {ym: _month_weights(ym[0], ym[1], meets) for ym in px}
    while True:
        cand = []
        for ym, w in w_all.items():
            unknown = [k for k in w if k not in r]
            if len(unknown) == 1:
                cand.append((w[unknown[0]], ym, unknown[0]))
        if not cand:
            return r
        _, ym, k = max(cand)
        known = sum(wt * r[j] for j, wt in w_all[ym].items() if j in r)
        r[k] = (100 - px[ym] - known) / w_all[ym][k]


def _distribution(r, n):
    """遞迴卷積:各會議相對現行水準的累積碼數機率分布。"""
    cur, out = {0: 1.0}, []
    for i in range(1, n + 1):
        move = (r[i] - r[i - 1]) / STEP
        lo = int(move // 1)
        frac = move - lo
        nxt = {}
        for k, p in cur.items():
            nxt[k + lo] = nxt.get(k + lo, 0) + p * (1 - frac)
            nxt[k + lo + 1] = nxt.get(k + lo + 1, 0) + p * frac
        cur = {k: v for k, v in nxt.items() if v > 1e-6}
        out.append(dict(cur))
    return out


def compute():
    """回傳 (快照日, 現行區間下緣bp, [(會議日, {區間bp: 機率})], {會議日: 預期EFFR})。"""
    effr, lo_bp = _fetch_effr()
    meets = _fetch_calendar()
    px, snap = _fetch_futures()
    r = _solve(px, meets, effr)
    n = max(k for k in r)                    # 只取解得出來的會議
    dists = _distribution(r, n)
    rows = [(meets[i], {lo_bp + k * 25: p for k, p in d.items()})
            for i, d in enumerate(dists)]
    rates = {meets[i]: r[i + 1] for i in range(n)}
    return snap, lo_bp, rows, rates, r[0]


def fetch(conn):
    try:
        snap, _lo, rows, rates, cur = compute()
    except Exception as e:
        print(f"[fetch] fedwatch: 失敗 — {e}")
        return ["fedwatch"]
    prob_rows = [(snap, mt.isoformat(), bp, p)
                 for mt, d in rows for bp, p in d.items()]
    rate_rows = [(snap, "current", cur)] + \
                [(snap, mt.isoformat(), v) for mt, v in rates.items()]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO fedwatch"
            " (data_date, meeting_date, bucket_bp, prob) VALUES (?,?,?,?)",
            prob_rows)
        conn.executemany(
            "INSERT OR REPLACE INTO fedwatch_rate"
            " (data_date, meeting_date, exp_effr) VALUES (?,?,?)", rate_rows)
    print(f"[fetch] fedwatch: {snap} 快照,{len(rows)} 次會議 / "
          f"{len(prob_rows)} 個區間")
    return []


# ------------------------------------------------------------------ 推播訊息
def _fmt_range(bp):
    return f"{bp / 100:.2f}-{(bp + 25) / 100:.2f}%"


def build_message(conn):
    """早報只講最近一次會議,但該次所有有機率的檔位都列出(不變、升降息、
    多碼),依目標區間由低到高排序 — 與 CME 表格的排法一致。"""
    row = conn.execute(
        "SELECT data_date, meeting_date FROM fedwatch"
        " ORDER BY data_date DESC, meeting_date ASC LIMIT 1").fetchone()
    base = conn.execute(
        "SELECT exp_effr FROM fedwatch_rate WHERE data_date=(SELECT"
        " MAX(data_date) FROM fedwatch) AND meeting_date='current'").fetchone()
    if not row or not base:
        return None, {}
    snap, meeting = row
    cur_bp = int(base[0] * 100 // 25 * 25)      # EFFR 落在哪個 25bp 網格
    buckets = conn.execute(
        "SELECT bucket_bp, prob FROM fedwatch WHERE data_date=? AND"
        " meeting_date=? AND prob>=0.0005 ORDER BY bucket_bp",
        (snap, meeting)).fetchall()
    if not buckets:
        return None, {}
    prev_snap = conn.execute(
        "SELECT MAX(data_date) FROM fedwatch WHERE data_date<?",
        (snap,)).fetchone()[0]
    prev = dict(conn.execute(
        "SELECT bucket_bp, prob FROM fedwatch WHERE data_date=? AND"
        " meeting_date=?", (prev_snap, meeting)).fetchall()) if prev_snap else {}
    md = dt.date.fromisoformat(meeting)
    lines = [f"🏛 FedWatch {md.month}/{md.day} 會議"]
    for bp, p in buckets:
        steps = (bp - cur_bp) // 25
        label = "不變" if steps == 0 else (f"升{steps}碼" if steps > 0
                                           else f"降{-steps}碼")
        chg = f"（{(p - prev[bp]) * 100:+.1f}pp）" if bp in prev else ""
        lines.append(f"{label} {_fmt_range(bp)} {p * 100:.1f}%{chg}")
    lines.append(f"現行 {_fmt_range(cur_bp)}、EFFR {base[0]:.2f}%")
    return "\n".join(lines), {"fedwatch": snap}
