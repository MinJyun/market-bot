"""抓證交所官方券商名稱對照表 → data/broker_names.json。

網頁只有 13 個重點分點寫死了中文名,其餘 889 個只顯示代號,難以判讀。
兩支官方端點合併(皆免金鑰):
- OpenData_BRK02:分公司(814 筆),欄位為中文
- brokerService/brokerList:總公司(64 筆),欄位為英文 Code/Name

用法:  python3 tools_broker_names.py
"""
import json
import sqlite3
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(__file__).parent / "data" / "broker_names.json"
UA = {"User-Agent": "Mozilla/5.0"}
BRANCH = "https://openapi.twse.com.tw/v1/opendata/OpenData_BRK02"
HEAD = "https://openapi.twse.com.tw/v1/brokerService/brokerList"


def get(url):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=40))


def db_codes():
    """DB 裡實際出現過的分點代號,按前綴分組(只為這些代號補名稱)。"""
    db = Path(__file__).parent / "data" / "broker.db"
    if not db.exists():
        return {}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.execute("PRAGMA busy_timeout=20000")
    out = defaultdict(list)
    # 取全期間出現過的分點(broker_totals),不是只取最新一日 —— 缺名稱的
    # 冷門分點常常最近沒交易,只看最新一日會漏掉。
    try:
        rows = c.execute("SELECT bno FROM broker_totals").fetchall()
    except sqlite3.OperationalError:
        rows = c.execute("SELECT DISTINCT bno FROM broker_daily").fetchall()
    for (b,) in rows:
        out[b[:2]].append(b)
    c.close()
    return out


def main():
    global _seen_prefixes
    _seen_prefixes = db_codes()
    names = {}
    for d in get(BRANCH):
        code = d.get("證券商代號")
        name = (d.get("證券商名稱") or "").replace(" ", "")
        if code and name:
            names[code] = name
    n_branch = len(names)
    # 總公司端點用英文欄位名(Code/Name),與分公司那支不同
    for d in get(HEAD):
        code, name = d.get("Code"), (d.get("Name") or "").replace(" ", "")
        if code and name:
            names.setdefault(code, name)
    n_official = len(names)
    # 官方表仍缺部分代號(多為總公司或自營部)。同一券商的分點代號共用前兩碼,
    # 故用同前綴分公司名的「-」前段推導公司名 —— 只補到公司層級,不猜分點名,
    # UI 一律把代號顯示在名稱旁,不會誤認成特定分點。
    by_prefix = defaultdict(Counter)
    for code, name in names.items():
        by_prefix[code[:2]][name.split("-")[0]] += 1
    inferred = 0
    for pfx, cand in by_prefix.items():
        base = cand.most_common(1)[0][0]
        for code in _seen_prefixes.get(pfx, ()):
            if code not in names:
                names[code] = base
                inferred += 1
    OUT.write_text(json.dumps(names, ensure_ascii=False, indent=0),
                   encoding="utf-8")
    print(f"官方 {n_official} 筆 + 前綴推導 {inferred} 筆 = {len(names)} 筆"
          f" → {OUT}")


if __name__ == "__main__":
    main()
