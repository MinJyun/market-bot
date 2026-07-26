"""資料源:我的持股籌碼(trade-sync 持股 × 個股籌碼 → Google Sheets)。

從 trade-sync 每日寫入的「每日持股 YYYY」tab 讀持股清單,交叉 market-bot
已入庫的個股籌碼(inst_stock 三大法人買賣超、margin_stock 資券/借券餘額),
把逐股籌碼變化 append 到同一本試算表的「持股籌碼 YYYY」tab(分年度、自動
建立、同日不重寫)。

- fetch:整份「每日持股」tab 匯入 my_holdings 表(冪等),持股歷史齊備後
  report 能一次補出過去的籌碼列。
- report:對每個有籌碼資料的交易日,取「該日(含)以前最近一天」的持股組列
  寫入 Sheet;籌碼日早於最早持股日則跳過。
- **個人持股不進 LINE 推播**(訊息會被轉發),build_message 恆回 None。

憑證與試算表 ID 直接沿用 trade-sync/.env(GOOGLE_SERVICE_ACCOUNT_JSON、
GOOGLE_SHEET_ID),不另存一份。ETF 持股無三大法人資料(inst_stock 只入庫
純股票),該幾欄留空。
對外契約:NAME / init / fetch(conn) / report(conn) / build_message(conn)。
"""
import json
from pathlib import Path

NAME = "my_chips"
ENV_PATH = Path(__file__).resolve().parent.parent.parent / "trade-sync" / ".env"
HOLD_TAB = "每日持股"    # trade-sync 分年度 tab 前綴
OUT_TAB = "持股籌碼"
HEADER = ["日期", "股名", "持有股數", "外資買賣超(張)", "投信(張)", "自營(張)",
          "合計(張)", "融資餘額(張)", "融資增減", "融券餘額(張)", "融券增減",
          "借券賣出(張)", "借券增減"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS my_holdings (
    data_date TEXT NOT NULL,   -- YYYY-MM-DD
    code      TEXT NOT NULL,
    name      TEXT,
    shares    REAL,            -- 各帳戶合計股數
    PRIMARY KEY (data_date, code)
);
"""


def init(conn):
    conn.executescript(SCHEMA)


def _env():
    vals = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip("'\"")
    return vals


def _spreadsheet():
    import gspread
    from google.oauth2.service_account import Credentials
    env = _env()
    creds = Credentials.from_service_account_info(
        json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(env["GOOGLE_SHEET_ID"])


def _iso(s):
    """'YYYY/M/D' → 'YYYY-MM-DD';解析失敗回 None。"""
    parts = s.strip().replace("/", "-").split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"


# ================================================================ 持股匯入
def fetch(conn):
    try:
        ss = _spreadsheet()
        agg = {}  # (dd, code) -> [name, shares]
        for ws in ss.worksheets():
            if not ws.title.startswith(HOLD_TAB):
                continue
            for row in ws.get_all_values()[1:]:
                dd = _iso(row[0]) if row else None
                if not dd or len(row) < 4:
                    continue
                stock = row[2].strip()
                code = stock.split()[0]
                name = stock.split(maxsplit=1)[1] if " " in stock else ""
                shares = float(row[3].replace(",", "") or 0)
                if (dd, code) in agg:
                    agg[(dd, code)][1] += shares
                else:
                    agg[(dd, code)] = [name, shares]
        if not agg:
            print(f"[fetch] my_chips: 「{HOLD_TAB}」tab 無持股資料")
            return ["my_chips"]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO my_holdings VALUES (?,?,?,?)",
                [(dd, code, n, s) for (dd, code), (n, s) in agg.items()])
        latest = max(dd for dd, _ in agg)
        n_latest = sum(1 for (dd, _) in agg if dd == latest)
        print(f"[fetch] my_chips: 持股 {latest} 共 {n_latest} 檔"
              f"(歷史 {len(agg)} 列)")
        return []
    except Exception as e:
        print(f"[fetch] my_chips: 失敗 — {e}")
        return ["my_chips"]


# ================================================================ Sheet 輸出
def _chip_row(conn, dd, code, name, shares):
    """組一列 HEADER 對應的值;無資料的欄位留空字串。

    先查上市表(inst_stock/margin_stock),沒有再查上櫃表
    (inst_otc_stock/margin_otc_stock);兩市場欄位語意與單位相同。
    """
    inst = conn.execute(
        "SELECT foreign_net, trust_net, dealer_net, total_net "
        "FROM inst_stock WHERE data_date=? AND code=?", (dd, code)).fetchone()
    if not inst:
        inst = conn.execute(
            "SELECT foreign_net, trust_net, dealer_net, total_net "
            "FROM inst_otc_stock WHERE data_date=? AND code=?",
            (dd, code)).fetchone()
    ms = conn.execute(
        "SELECT fin_prev, fin_bal, short_prev, short_bal, sbl_prev, sbl_bal "
        "FROM margin_stock WHERE data_date=? AND code=?", (dd, code)).fetchone()
    if not ms:
        ms = conn.execute(
            "SELECT fin_prev, fin_bal, short_prev, short_bal, sbl_prev, sbl_bal "
            "FROM margin_otc_stock WHERE data_date=? AND code=?",
            (dd, code)).fetchone()
    # 兩者皆無仍寫一列空值,缺口看得見

    def lots(v):  # 股 → 張
        return round(v / 1000)

    row = [dd.replace("-", "/"), f"{code} {name}".strip(), int(shares)]
    row += [lots(v) for v in inst] if inst else [""] * 4
    if ms:
        fp, fb, sp, sb, bp, bb = ms
        row += [fb, fb - fp, sb, sb - sp, lots(bb), lots(bb - bp)]
    else:
        row += [""] * 6
    return row


def _ensure_formulas(ss, year):
    """「每日持股 {year}」J 欄起放 ARRAYFORMULA,同畫面帶出該年持股籌碼。

    以 日期|股名 為 key VLOOKUP「持股籌碼 {year}」;只在 J1 尚未設定時寫入
    (冪等),trade-sync 之後 append 的新列由 ARRAYFORMULA 自動帶出。
    """
    import gspread
    try:
        ws = ss.worksheet(f"{HOLD_TAB} {year}")
    except gspread.WorksheetNotFound:
        return
    chip_cols = HEADER[3:]                      # 10 欄:外資買賣超(張)~借券增減
    need = 9 + len(chip_cols)                   # A~I 9 欄 + 籌碼欄
    if ws.col_count < need:
        ws.add_cols(need - ws.col_count)        # 只有 9 欄時連讀 J1 都會 400
    elif ws.acell("J1").value:
        return
    out = f"{OUT_TAB} {year}"
    formulas = []
    for i in range(len(chip_cols)):
        c = chr(ord("D") + i)                   # 持股籌碼 tab 的 D~M 欄
        formulas.append(
            '=ARRAYFORMULA(IF($A$2:$A="",,IFERROR(VLOOKUP('
            'TEXT($A$2:$A,"YYYY/MM/DD")&"|"&$C$2:$C,'
            f"{{TEXT('{out}'!$A$2:$A,\"YYYY/MM/DD\")&\"|\"&'{out}'!$B$2:$B,"
            f"'{out}'!${c}$2:${c}}},2,0),)))")
    ws.update("J1", [chip_cols])
    ws.update("J2", [formulas], value_input_option="USER_ENTERED")
    # 新欄會繼承 I 欄(損益率)的百分比格式,改回一般數字
    ws.format("J2:S", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
    print(f"[report] my_chips: 已在「{HOLD_TAB} {year}」J 欄設定籌碼公式")


def report(conn):
    hold_dates = sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM my_holdings"))
    if not hold_dates:
        print("[report] my_chips: 尚無持股資料(先跑 fetch),略過")
        return
    chip_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM margin_stock ORDER BY data_date")]
    try:
        ss = _spreadsheet()
        import gspread
        by_year = {}
        for dd in chip_dates:
            if dd < hold_dates[0]:
                continue  # 籌碼日早於最早持股紀錄
            by_year.setdefault(dd[:4], []).append(dd)
        total = 0
        for year, dates in sorted(by_year.items()):
            title = f"{OUT_TAB} {year}"
            try:
                ws = ss.worksheet(title)
            except gspread.WorksheetNotFound:
                ws = ss.add_worksheet(title=title, rows=2000, cols=len(HEADER))
                ws.update("A1", [HEADER])
                print(f"[report] my_chips: 建立新工作表「{title}」")
            done = {_iso(v) for v in ws.col_values(1)[1:] if v.strip()}
            rows = []
            for dd in dates:
                if dd in done:
                    continue
                hd = max(h for h in hold_dates if h <= dd)
                for code, name, shares in conn.execute(
                        "SELECT code, name, shares FROM my_holdings "
                        "WHERE data_date=? ORDER BY code", (hd,)):
                    rows.append(_chip_row(conn, dd, code, name, shares))
            if rows:
                ws.append_rows(rows, value_input_option="USER_ENTERED")
                total += len(rows)
            _ensure_formulas(ss, year)
        print(f"[report] my_chips: 寫入 {total} 列到「{OUT_TAB}」")
    except Exception as e:
        print(f"[report] my_chips: 失敗 — {e}")


def build_message(conn):
    return None, {}  # 個人持股不進 LINE(訊息會被轉發)
