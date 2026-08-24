"""晚間籌碼儀表板:DB → HTML 模板 → headless Chrome 截圖 PNG → LINE 圖片。

與文字訊息同一份 DB 資料、固定模板渲染(非 AI 產圖),數字不會漂。流程:
render(產 HTML+截圖) → publish(單獨 commit/push PNG,LINE 要能立刻抓到
raw.githubusercontent URL) → notify.push_image。任何一步失敗只是當天沒圖,
文字訊息不受影響。渲染依賴本機 Chrome 與 Pillow(裁底部白邊)。

顏色循台股慣例:紅=漲/買超、綠=跌/賣超。
"""
import subprocess
from datetime import date
from pathlib import Path

from core import store

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
RAW_URL = ("https://raw.githubusercontent.com/MinJyun/market-bot/main/"
           "reports/{dd}/{name}")
WIDTH = 1020

CSS = """
* { margin:0; padding:0; box-sizing:border-box;
    font-family:'PingFang TC','Heiti TC',sans-serif; }
body { width:1020px; background:#eef1f6; padding:10px; }
.row { display:flex; gap:10px; margin-bottom:10px; }
.card { background:#fff; border-radius:10px; overflow:hidden; flex:1;
        box-shadow:0 1px 3px rgba(0,0,0,.12); }
.hd { background:#1a2f5e; color:#fff; font-size:17px; font-weight:700;
      padding:8px 14px; }
.hd small { font-weight:400; opacity:.85; margin-left:8px; font-size:13px; }
.bd { padding:10px 14px; }
.up { color:#d0342c; } .dn { color:#1a8a4a; } .flat { color:#666; }
table { width:100%; border-collapse:collapse; font-size:15px; }
th { color:#555; font-weight:600; text-align:left; padding:5px 4px;
     border-bottom:1px solid #dde; font-size:13px; }
td { padding:5px 4px; border-bottom:1px solid #f0f2f6; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.big { font-size:34px; font-weight:800; letter-spacing:.5px; }
.mid { font-size:20px; font-weight:700; }
.lbl { color:#667; font-size:13px; margin-top:8px; }
.bars { display:flex; align-items:flex-end; gap:14px; height:120px;
        margin:14px 6px 4px; }
.bargrp { flex:1; display:flex; flex-direction:column; align-items:center;
          height:100%; justify-content:flex-end; }
.barv { font-size:13px; font-weight:700; margin-bottom:2px; }
.bar { width:34px; border-radius:3px 3px 0 0; }
.barn { font-size:13px; color:#555; margin-top:4px; }
.cols { display:flex; gap:10px; }
.col { flex:1; border:1px solid #e2e6ee; border-radius:8px; padding:8px 10px; }
.colhd { text-align:center; font-weight:700; font-size:16px; padding:4px 0 6px; }
.kv { display:flex; justify-content:space-between; font-size:14.5px;
      padding:2.5px 0; gap:6px; }
.kv span:first-child { white-space:nowrap; color:#445; }
.kv b { font-variant-numeric:tabular-nums; text-align:right;
        white-space:nowrap; }
.sep { border-top:1px dashed #ccd3e0; margin:6px 0; }
.donutwrap { display:flex; align-items:center; gap:14px; flex:1; }
.donut { width:110px; height:110px; border-radius:50%; position:relative; }
.donut .hole { position:absolute; inset:18px; background:#fff;
               border-radius:50%; display:flex; align-items:center;
               justify-content:center; font-weight:800; font-size:17px; }
.legend { font-size:13.5px; line-height:1.7; }
.dot { display:inline-block; width:10px; height:10px; border-radius:2px;
       margin-right:5px; }
.note { color:#778; font-size:12px; padding:2px 6px 8px; }
.mgrid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
.mtile { border:1px solid #e2e6ee; border-radius:8px; padding:9px 12px; }
.mname { color:#667; font-size:13.5px; }
.mval { font-size:27px; font-weight:800; font-variant-numeric:tabular-nums;
        line-height:1.15; }
.mchg { font-size:14px; font-weight:700; }
.etfcol { column-count:2; column-gap:10px; }
.etfcol .card { flex:none; margin-bottom:10px; break-inside:avoid; }
.etfrow { display:flex; justify-content:space-between; gap:10px;
          font-size:14px; padding:2.5px 0; }
.etfrow span { color:#334; }
.etfrow b { font-variant-numeric:tabular-nums; white-space:nowrap; }
.etfsub { font-size:13px; font-weight:700; color:#556; margin:6px 0 1px; }
.etffut { font-size:13px; color:#7a5; font-weight:700; padding:2px 0; }
.etfflat { color:#889; font-size:13.5px; padding:3px 0; }
.banner { background:#1a2f5e; color:#fff; font-size:19px; font-weight:800;
          padding:9px 14px; border-radius:10px; margin-bottom:10px; }
.banner small { font-weight:400; opacity:.85; font-size:14px; margin-left:8px; }
.oi { font-weight:400; font-size:12.5px; color:#889; margin-left:6px; }
.subnote { font-size:12px; color:#556; padding:7px 2px 1px; text-align:center; }
.muted { color:#8894a5; font-size:11.5px; margin-left:5px; }
.chgsmall { font-size:11.5px; margin-left:4px; }
.cols table { font-size:14px; }
.cols th, .cols td { padding:4px 4px; }
"""


def _n(v, dec=0):
    return f"{v:,.{dec}f}"


def _sgn(v, dec=0, suffix=""):
    cls = "up" if v > 0 else ("dn" if v < 0 else "flat")
    return f'<span class="{cls}">{v:+,.{dec}f}{suffix}</span>'


def _bs(v, dec=1):
    """買超紅/賣超綠 + 文字。"""
    if v >= 0:
        return f'<span class="up">買超 {v / 1e8:,.{dec}f}</span>'
    return f'<span class="dn">賣超 {v / 1e8:,.{dec}f}</span>'


def _bars(vals, names, unit="億"):
    mx = max(abs(v) for v in vals) or 1
    out = ['<div class="bars">']
    for v, n in zip(vals, names):
        h = max(6, int(abs(v) / mx * 88))
        color = "#d0342c" if v >= 0 else "#1a8a4a"
        out.append(
            f'<div class="bargrp"><div class="barv">{v:,.1f}</div>'
            f'<div class="bar" style="height:{h}px;background:{color}"></div>'
            f'<div class="barn">{n}</div></div>')
    out.append("</div>")
    return "".join(out)


def _donut(title, ratio, chg, sell, buy, sell_lbl, buy_lbl):
    pct = min(max(ratio / (ratio + 100) * 100, 0), 100)  # 賣方占比
    return f"""
    <div class="donutwrap">
      <div>
        <div class="lbl">{title}</div>
        <div class="mid">{ratio:.2f}%</div>
        <div style="font-size:14px">{_sgn(chg, 2)}</div>
      </div>
      <div class="donut" style="background:conic-gradient(#d0342c 0 {pct:.1f}%,#3358a8 {pct:.1f}% 100%)">
        <div class="hole">{ratio:.1f}%</div>
      </div>
      <div class="legend">
        <span class="dot" style="background:#d0342c"></span>{sell_lbl} {sell:,.0f} 口<br>
        <span class="dot" style="background:#3358a8"></span>{buy_lbl} {buy:,.0f} 口
      </div>
    </div>"""


# ================================================================ 各區塊
def _sec_index(conn):
    rows = conn.execute(
        "SELECT data_date, close, change_pts, change_pct, amount "
        "FROM market_index ORDER BY data_date DESC LIMIT 2").fetchall()
    if not rows:
        return "", ""
    dd, close, pts, pct, amount = rows[0]
    arrow = "▲" if pts > 0 else ("▼" if pts < 0 else "—")
    cls = "up" if pts > 0 else ("dn" if pts < 0 else "flat")
    amt_chg = ""
    if len(rows) > 1 and rows[1][4]:
        amt_chg = f'<div style="font-size:14px">較前日 {_sgn((amount - rows[1][4]) / 1e8)} 億</div>'
    html = f"""
    <div class="card" style="flex:0 0 300px">
      <div class="hd">📈 加權指數<small>{dd[5:].replace('-', '/')}</small></div>
      <div class="bd">
        <div class="lbl">收盤</div>
        <div class="big {cls}">{_n(close, 2)}</div>
        <div class="{cls}" style="font-size:16px;font-weight:700">{arrow} {abs(pts):,.2f}（{pct:+.2f}%）</div>
        <div class="lbl">成交金額</div>
        <div class="mid">{amount / 1e8:,.0f} 億</div>{amt_chg}
      </div>
    </div>"""
    return html, dd


def _sec_inst(conn, table, title):
    row = conn.execute(
        f"SELECT data_date, foreign_net, trust_net, dealer_net, total_net "
        f"FROM {table} ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return ""
    dd, fo, tr, de, to = row
    rows = "".join(
        f"<tr><td>{n}</td><td class='num'>{_bs(v)}</td></tr>"
        for n, v in (("外資", fo), ("投信", tr), ("自營", de), ("合計", to)))
    return f"""
    <div class="card">
      <div class="hd">👥 {title}<small>{dd[5:].replace('-', '/')}</small></div>
      <div class="bd">
        <table><tr><th>法人</th><th class="num">買賣超(億)</th></tr>{rows}</table>
        {_bars([fo / 1e8, tr / 1e8, de / 1e8, to / 1e8], ["外資", "投信", "自營", "合計"])}
      </div>
    </div>"""


def _sec_margin(conn):
    from sources.margin import _maintenance_ratio
    row = conn.execute(
        "SELECT data_date, margin_bal, margin_prev, short_bal, short_prev, "
        "amt_bal, amt_prev, sbl_bal, sbl_prev "
        "FROM margin ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return ""
    dd, mbal, mprev, sbal, sprev, abal, aprev, bbal, bprev = row
    ratio = _maintenance_ratio(conn, dd)
    prev_dd = conn.execute("SELECT MAX(data_date) FROM margin WHERE data_date<?",
                           (dd,)).fetchone()[0]
    pratio = _maintenance_ratio(conn, prev_dd) if prev_dd else None
    items = [("融資餘額", f"{_n(mbal)} 張", _sgn(mbal - mprev)),
             ("融資金額", f"{abal / 1e5:,.1f} 億", _sgn((abal - aprev) / 1e5, 1, " 億"))]
    if ratio:
        items.append(("融資維持率(估)", f"{ratio:.1f} %",
                      _sgn(ratio - pratio, 1) if pratio else "—"))
    items += [("融券餘額", f"{_n(sbal)} 張", _sgn(sbal - sprev)),
              ("借券賣出餘額", f"{_n(bbal / 1000)} 張", _sgn((bbal - bprev) / 1000))]
    rows = "".join(f"<tr><td>{n}</td><td class='num'><b>{v}</b></td>"
                   f"<td class='num'>{c}</td></tr>" for n, v, c in items)
    return f"""
    <div class="card" style="flex:0 0 360px">
      <div class="hd">⚖️ 融資融券／借券<small>{dd[5:].replace('-', '/')}</small></div>
      <div class="bd"><table>
        <tr><th>項目</th><th class="num">數值</th><th class="num">較前日</th></tr>
        {rows}</table></div>
    </div>"""


def _trow(label, val, chg):
    """機構籌碼表列:身份 | 淨部位(有號、紅多綠空) | 前日Δ。"""
    c = _sgn(chg) if chg is not None else '<span class="flat">—</span>'
    return (f'<tr><td>{label}</td><td class="num">{_sgn(val)}</td>'
            f'<td class="num">{c}</td></tr>')


def _trade_table(full, night, title="今日交易(口)"):
    """三大法人今日交易淨額拆日盤/夜盤:日盤=全日−夜盤,凸顯日盤方向。
    full=(外資,投信,自營)全日交易淨;night 同結構的夜盤,無夜盤資料則傳 None。"""
    rows = []
    for i, who in ((0, "外資"), (1, "投信"), (2, "自營")):
        fd = full[i] or 0
        if night is not None:
            ni = night[i] or 0
            rows.append(f'<tr><td>{who}</td><td class="num">{_sgn(fd - ni)}</td>'
                        f'<td class="num">{_sgn(ni)}</td>'
                        f'<td class="num">{_sgn(fd)}</td></tr>')
        else:
            rows.append(f'<tr><td>{who}</td>'
                        f'<td class="num flat">—</td><td class="num flat">—</td>'
                        f'<td class="num">{_sgn(fd)}</td></tr>')
    return (f'<div class="colhd" style="margin-top:8px">{title}</div>'
            '<table><tr><th>身份</th><th class="num">日盤</th>'
            '<th class="num">夜盤</th><th class="num">全日</th></tr>'
            + "".join(rows) + '</table>')


def _sec_futures(conn):
    from sources.futures_traders import CONTRACTS, _foreign_cost
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM futures_lt ORDER BY data_date DESC LIMIT 2")]
    idates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM futures_inst ORDER BY data_date DESC LIMIT 2")]
    if not dates and not idates:
        return ""
    curr = dates[0] if dates else idates[0]
    cost = _foreign_cost(conn)
    cols = []
    for disp in CONTRACTS.values():
        lt = conn.execute("SELECT buy5,buy10,sell5,sell10,oi FROM futures_lt "
                          "WHERE contract=? AND data_date=?",
                          (disp, dates[0] if dates else "")).fetchone()
        ltp = conn.execute("SELECT buy5,buy10,sell5,sell10 FROM futures_lt "
                           "WHERE contract=? AND data_date=?",
                           (disp, dates[1] if len(dates) > 1 else "")).fetchone()
        ic = conn.execute("SELECT foreign_net,trust_net,dealer_net FROM futures_inst "
                          "WHERE contract=? AND data_date=?",
                          (disp, idates[0] if idates else "")).fetchone()
        ip = conn.execute("SELECT foreign_net,trust_net,dealer_net FROM futures_inst "
                          "WHERE contract=? AND data_date=?",
                          (disp, idates[1] if len(idates) > 1 else "")).fetchone()
        if not lt and not ic:
            continue
        rows = []
        if ic:
            for i, who in ((0, "外資"), (1, "投信"), (2, "自營")):
                rows.append(_trow(who, ic[i], (ic[i] - ip[i]) if ip else None))
        oi_txt = ""
        if lt:
            b5, b10, s5, s10, oi = lt
            n5, n10 = b5 - s5, b10 - s10
            c5 = (n5 - (ltp[0] - ltp[2])) if ltp else None
            c10 = (n10 - (ltp[1] - ltp[3])) if ltp else None
            rows.append(_trow("前五大", n5, c5))
            rows.append(_trow("前十大", n10, c10))
            oi_txt = f'<span class="oi">未平倉 {oi:,.0f} 口</span>'
        table = ('<table><tr><th>身份</th><th class="num">淨部位(口)</th>'
                 '<th class="num">前日Δ</th></tr>' + "".join(rows) + '</table>')
        # 台指期:今日交易拆日盤/夜盤(全日交易淨額 − 夜盤交易淨額)
        trade_html = ""
        if disp == "台指期":
            ft = conn.execute(
                "SELECT foreign_trade,trust_trade,dealer_trade FROM futures_inst "
                "WHERE contract='台指期' AND data_date=?",
                (idates[0] if idates else "",)).fetchone()
            if ft and ft[0] is not None:
                nt = conn.execute(
                    "SELECT foreign_net,trust_net,dealer_net FROM fut_night_inst "
                    "WHERE contract='台指' AND data_date=?",
                    (idates[0],)).fetchone()
                trade_html = _trade_table(ft, nt)
        costhtml = ""
        if disp == "台指期" and cost:
            c, settle, pos, pnl, cstart = cost
            cls = "up" if pnl >= 0 else "dn"
            costhtml = (f'<div class="subnote">外資估算成本 <b>{_n(c)}</b>｜結算 {_n(settle)}'
                        f'｜<span class="{cls}">{"浮盈" if pnl >= 0 else "浮虧"} '
                        f'{abs(pnl):,.0f} 億</span>'
                        f'<span class="muted">自{cstart[5:]}</span></div>')
        cols.append(f'<div class="col"><div class="colhd">{disp}{oi_txt}</div>'
                    f'{table}{trade_html}{costhtml}</div>')
    return f"""
    <div class="card">
      <div class="hd">📊 期貨機構籌碼<small>{curr[5:].replace('-', '/')}(未平倉多空淨額/口,+多 −空;前五/十大為特定法人淨)</small></div>
      <div class="bd"><div class="cols">{''.join(cols)}</div></div>
    </div>"""


def _sec_options(conn):
    idd = conn.execute("SELECT MAX(data_date) FROM options_inst").fetchone()[0]
    if not idd:
        return ""
    ic = conn.execute("SELECT foreign_net,trust_net,dealer_net FROM options_inst "
                      "WHERE data_date=?", (idd,)).fetchone()
    ipd = conn.execute("SELECT MAX(data_date) FROM options_inst WHERE data_date<?",
                       (idd,)).fetchone()[0]
    ip = conn.execute("SELECT foreign_net,trust_net,dealer_net FROM options_inst "
                      "WHERE data_date=?", (ipd,)).fetchone() if ipd else None
    cpdd = conn.execute("SELECT MAX(data_date) FROM options_inst_cp").fetchone()[0]
    cps = {r[0]: r[1:] for r in conn.execute(
        "SELECT cp, foreign_net, trust_net, dealer_net FROM options_inst_cp "
        "WHERE data_date=?", (cpdd,))} if cpdd else {}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM options_lt ORDER BY data_date DESC LIMIT 2")]

    cppd = conn.execute("SELECT MAX(data_date) FROM options_inst_cp WHERE data_date<?",
                        (cpdd,)).fetchone()[0] if cpdd else None
    cpp = {r[0]: r[1:] for r in conn.execute(
        "SELECT cp, foreign_net, trust_net, dealer_net FROM options_inst_cp "
        "WHERE data_date=?", (cppd,))} if cppd else {}

    left = ['<div class="col" style="flex:0 0 360px">']
    if ic:
        rows = "".join(
            _trow(who, ic[i], (ic[i] - ip[i]) if ip else None)
            for i, who in ((0, "外資"), (1, "投信"), (2, "自營")))
        left.append('<div class="colhd">三大法人淨(不分買賣權)</div>'
                    '<table><tr><th>身份</th><th class="num">淨部位(口)</th>'
                    '<th class="num">前日Δ</th></tr>' + rows + '</table>')
    # 今日交易拆日盤/夜盤(全日交易淨額 − 夜盤Call+Put交易淨額),不分買賣權
    ot = conn.execute(
        "SELECT foreign_trade,trust_trade,dealer_trade FROM options_inst "
        "WHERE data_date=?", (idd,)).fetchone()
    if ot and ot[0] is not None:
        nr = conn.execute("SELECT foreign_net,trust_net,dealer_net "
                          "FROM fut_night_opt WHERE data_date=?", (idd,)).fetchall()
        onight = [sum(r[i] for r in nr) for i in range(3)] if nr else None
        left.append(_trade_table(ot, onight, "今日交易(不分買賣權,口)"))
    if cps:
        def cpcell(side, i):
            v = cps[side][i]
            if side in cpp:
                return _sgn(v) + f'<span class="chgsmall">{_sgn(v - cpp[side][i])}</span>'
            return _sgn(v)
        crows = ""
        for i, who in ((0, "外資"), (1, "投信"), (2, "自營")):
            call = cpcell("買權", i) if "買權" in cps else "—"
            put = cpcell("賣權", i) if "賣權" in cps else "—"
            crows += (f'<tr><td>{who}</td><td class="num">{call}</td>'
                      f'<td class="num">{put}</td></tr>')
        left.append('<div class="colhd" style="margin-top:8px">Call／Put 淨OI(值+前日Δ)</div>'
                    '<table><tr><th>身份</th><th class="num">Call</th>'
                    '<th class="num">Put</th></tr>' + crows + '</table>')
    left.append("</div>")

    cols = ["".join(left)]
    for disp, tone in (("臺指Call", "#eef4ff"), ("臺指Put", "#fdeeee")):
        lt = conn.execute("SELECT buy5,buy10,sell5,sell10,oi FROM options_lt "
                          "WHERE contract=? AND data_date=?",
                          (disp, dates[0] if dates else "")).fetchone()
        if not lt:
            continue
        ltp = conn.execute("SELECT buy5,buy10,sell5,sell10 FROM options_lt "
                           "WHERE contract=? AND data_date=?",
                           (disp, dates[1] if len(dates) > 1 else "")).fetchone()
        b5, b10, s5, s10, oi = lt

        def bscell(v, i):
            return (f'{v:,.0f}<span class="chgsmall">{_sgn(v - ltp[i])}</span>'
                    if ltp else f'{v:,.0f}')
        cols.append(f"""
        <div class="col" style="background:{tone}">
          <div class="colhd">{disp}<span class="oi">未平倉 {oi:,.0f} 口</span></div>
          <table><tr><th>特定法人</th><th class="num">買(前日Δ)</th><th class="num">賣(前日Δ)</th></tr>
            <tr><td>前五大</td><td class="num">{bscell(b5, 0)}</td><td class="num">{bscell(s5, 2)}</td></tr>
            <tr><td>前十大</td><td class="num">{bscell(b10, 1)}</td><td class="num">{bscell(s10, 3)}</td></tr>
          </table>
        </div>""")
    return f"""
    <div class="card">
      <div class="hd">🧩 臺指選擇權機構籌碼<small>{idd[5:].replace('-', '/')}(未平倉淨口數;前五/十大為所有契約特定法人)</small></div>
      <div class="bd"><div class="cols">{''.join(cols)}</div></div>
    </div>"""


def _sec_pc(conn):
    rows = conn.execute(
        "SELECT data_date, put_vol, call_vol, vol_ratio, put_oi, call_oi, oi_ratio "
        "FROM pc_ratio ORDER BY data_date DESC LIMIT 2").fetchall()
    if not rows:
        return ""
    dd, pv, cv, vr, po, co, oir = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    return f"""
    <div class="card">
      <div class="hd">🍩 臺指選擇權 Put/Call Ratio<small>{dd[5:].replace('-', '/')}</small></div>
      <div class="bd"><div class="cols">
        {_donut("成交量比", vr, (vr - prev[3]) if prev else 0, pv, cv, "賣(Put)", "買(Call)")}
        {_donut("未平倉比", oir, (oir - prev[6]) if prev else 0, po, co, "賣(Put)", "買(Call)")}
      </div></div>
    </div>"""


# ================================================================ 早上:總經＋夜盤
def _sec_macro(conn):
    def latest2(sym):
        return conn.execute(
            "SELECT data_date, value FROM macro WHERE symbol=? "
            "ORDER BY data_date DESC LIMIT 2", (sym,)).fetchall()
    # (symbol, 顯示名, 小數位, 後綴, 漲跌用 %(否則絕對值,如殖利率百分點))
    items = [("USDTWD", "美元/台幣", 3, "", True), ("USDJPY", "美元/日圓", 2, "", True),
             ("DXY", "美元指數", 2, "", True), ("WTI", "WTI原油", 2, "", True),
             ("GOLD", "黃金", 0, "", True), ("US10Y", "美債10Y", 2, "%", False),
             ("VIX", "VIX", 2, "", True), ("SOX", "費半", 0, "", True),
             ("SPX", "S&P500", 0, "", True)]
    tiles = []
    for sym, name, dec, suffix, pct in items:
        rows = latest2(sym)
        if not rows:
            continue
        (_, v), prev = rows[0], (rows[1][1] if len(rows) > 1 else None)
        if prev is not None:
            chg = (v - prev) / prev * 100 if pct else v - prev
            cls = "up" if chg > 0 else ("dn" if chg < 0 else "flat")
            chg_txt = f"{chg:+.2f}%" if pct else f"{chg:+.2f}"
        else:
            cls, chg_txt = "flat", "—"
        tiles.append(
            f'<div class="mtile"><div class="mname">{name}</div>'
            f'<div class="mval {cls}">{v:,.{dec}f}{suffix}</div>'
            f'<div class="mchg {cls}">{chg_txt}</div></div>')
    if not tiles:
        return "", None
    fx = latest2("USDTWD")
    dd = fx[0][0] if fx else conn.execute(
        "SELECT MAX(data_date) FROM macro").fetchone()[0]
    html = f"""
    <div class="card">
      <div class="hd">🌍 國際總經<small>{dd[5:].replace('-', '/')}(美股為前一收盤)</small></div>
      <div class="bd"><div class="mgrid">{''.join(tiles)}</div></div>
    </div>"""
    return html, dd


def _sec_fut_night(conn):
    from sources.fut_night import CONTRACTS
    row = conn.execute(
        "SELECT data_date, close, chg, chg_pct, high, low, volume "
        "FROM fut_night ORDER BY data_date DESC LIMIT 1").fetchone()
    if not row:
        return "", None
    dd, close, chg, pct, high, low, vol = row
    cls = "up" if chg > 0 else ("dn" if chg < 0 else "flat")
    arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "—")
    inst = {r[0]: r[1:] for r in conn.execute(
        "SELECT contract, foreign_net, trust_net, dealer_net "
        "FROM fut_night_inst WHERE data_date=?", (dd,))}
    opt = {r[0]: r[1:] for r in conn.execute(
        "SELECT cp, foreign_net, trust_net, dealer_net "
        "FROM fut_night_opt WHERE data_date=?", (dd,))}

    tables = []
    if inst:
        rows_html, merged = [], [0.0, 0.0, 0.0]
        for _, (disp, weight) in CONTRACTS.items():
            if disp not in inst:
                continue
            fo, tr, de = inst[disp]
            rows_html.append(
                f"<tr><td>{disp}</td><td class='num'>{_sgn(fo)}</td>"
                f"<td class='num'>{_sgn(tr)}</td><td class='num'>{_sgn(de)}</td></tr>")
            for i, v in enumerate((fo, tr, de)):
                merged[i] += v * weight
        if len(inst) > 1:
            rows_html.append(
                f"<tr><td><b>合併</b></td><td class='num'>{_sgn(merged[0])}</td>"
                f"<td class='num'>{_sgn(merged[1])}</td>"
                f"<td class='num'>{_sgn(merged[2])}</td></tr>")
        tables.append(
            '<div class="lbl">期貨夜盤淨額(口)</div>'
            '<table><tr><th>契約</th><th class="num">外資</th>'
            '<th class="num">投信</th><th class="num">自營</th></tr>'
            + "".join(rows_html) + "</table>")
    if opt:
        rows_html = []
        for side, label in (("買權", "Call"), ("賣權", "Put")):
            if side in opt:
                fo, tr, de = opt[side]
                rows_html.append(
                    f"<tr><td>{label}</td><td class='num'>{_sgn(fo)}</td>"
                    f"<td class='num'>{_sgn(tr)}</td><td class='num'>{_sgn(de)}</td></tr>")
        tables.append(
            '<div class="lbl">臺指選擇權夜盤淨額(口)</div>'
            '<table><tr><th>買賣權</th><th class="num">外資</th>'
            '<th class="num">投信</th><th class="num">自營</th></tr>'
            + "".join(rows_html) + "</table>")

    html = f"""
    <div class="card">
      <div class="hd">🌙 台指期夜盤<small>{dd[5:].replace('-', '/')}(凌晨5點收盤,合併=台指+0.25小台+0.05微台)</small></div>
      <div class="bd">
        <div class="lbl">收盤</div>
        <div class="big {cls}">{close:,.0f}</div>
        <div class="{cls}" style="font-size:16px;font-weight:700">{arrow} {abs(chg):,.0f}（{pct:+.2f}%）</div>
        <div class="lbl">高 {high:,.0f}　低 {low:,.0f}　成交 {vol:,.0f} 口</div>
        {''.join(tables)}
      </div>
    </div>"""
    return html, dd


def render_morning(conn):
    """早上圖:國際總經 + 台指期夜盤。回傳 (png path, 資料日) 或 None。"""
    macro_html, mdd = _sec_macro(conn)
    fut_html, fdd = _sec_fut_night(conn)
    if not macro_html and not fut_html:
        return None
    body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>{CSS}</style></head><body>
    <div class="row">{macro_html}</div>
    <div class="row">{fut_html}</div>
    <div class="note">美股數值為台北時間清晨的美國收盤;夜盤歸屬次一交易日。</div>
    </body></html>"""
    return _screenshot(body, "morning.png"), (mdd or fdd)


# ================================================================ 產圖與遞送
def _screenshot(body, name):
    """HTML → Chrome 截圖 → 裁底部白邊,回傳 png path。"""
    out_dir = store.REPORTS / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / name.replace(".png", ".html")
    png_path = out_dir / name
    html_path.write_text(body, encoding="utf-8")
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
         f"--screenshot={png_path}", f"--window-size={WIDTH},2600",
         html_path.as_uri()],
        check=True, capture_output=True, timeout=60)
    # 裁掉底部多餘白/灰邊
    from PIL import Image
    img = Image.open(png_path)
    bg = img.getpixel((5, img.height - 5))
    bottom = img.height
    while bottom > 100:
        row = [img.getpixel((x, bottom - 1)) for x in range(0, img.width, 60)]
        if any(px != bg for px in row):
            break
        bottom -= 1
    img.crop((0, 0, img.width, min(bottom + 12, img.height))).save(png_path)
    return png_path


def render(conn):
    """晚間籌碼儀表板 HTML → 截圖,回傳 (png path, 資料日) 或 None。"""
    idx_html, dd = _sec_index(conn)
    if not dd:
        return None
    body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>{CSS}</style></head><body>
    <div class="row">{idx_html}{_sec_inst(conn, 'inst_spot', '三大法人現貨買賣超')}
                      {_sec_inst(conn, 'inst_otc', '上櫃三大法人買賣超')}</div>
    <div class="row">{_sec_margin(conn)}{_sec_pc(conn)}</div>
    <div class="row">{_sec_futures(conn)}</div>
    <div class="row">{_sec_options(conn)}</div>
    <div class="note">單位除另有說明外為口或張;金額為億元。外資成本與維持率為估算。</div>
    </body></html>"""
    return _screenshot(body, "dashboard.png"), dd


def render_chips_pre(conn):
    """先行版:期貨/選擇權機構籌碼 + Put/Call Ratio(盤後 15 點多公布即可出,
    不含融資融券等較晚公布項目)。回傳 (png path, 資料日) 或 None。"""
    fut, opt, pc = _sec_futures(conn), _sec_options(conn), _sec_pc(conn)
    if not (fut or opt or pc):
        return None
    body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>{CSS}</style></head><body>
    <div class="row">{pc}</div>
    <div class="row">{fut}</div>
    <div class="row">{opt}</div>
    <div class="note">先行版:期貨/選擇權機構籌碼與 Put/Call Ratio;融資融券、現貨法人等較晚公布項目見晚間完整版。</div>
    </body></html>"""
    dd = conn.execute("SELECT MAX(data_date) FROM futures_inst").fetchone()[0]
    return _screenshot(body, "chips_pre.png"), dd


def chips_pre_ready(conn):
    """先行版所需(期貨/選擇權/PC)當日資料是否全齊,避免推出殘缺的先行版。"""
    today = date.today().isoformat()
    for t in ("futures_inst", "futures_lt", "options_inst",
              "options_inst_cp", "options_lt", "pc_ratio"):
        if conn.execute(f"SELECT MAX(data_date) FROM {t}").fetchone()[0] != today:
            return False
    return True


# ================================================================ 主動式 ETF 持股異動
def _sec_etf(conn, etf):
    """單檔 ETF 相鄰兩資料日的持股異動卡片(數字口徑同 active_etf 文字版)。"""
    from sources import active_etf as ae
    dates = ae._recent_dates(conn, etf, 2)
    if len(dates) < 2:
        return ""
    curr_d, prev_d = dates
    curr, prev = ae._holdings(conn, etf, curr_d), ae._holdings(conn, etf, prev_d)
    fc, fp = ae._fund_day(conn, etf, curr_d), ae._fund_day(conn, etf, prev_d)

    def price(code):
        h = curr.get(code) or prev.get(code)
        return h["amount"] / h["shares"] if h["amount"] and h["shares"] else 0

    new, gone, inc, dec = [], [], [], []
    for code in set(curr) | set(prev):
        c, p = curr.get(code), prev.get(code)
        u = ae._unit((c or p)["name"])
        if p is None:
            new.append((c["amount"] or 0, c["name"], c["shares"], u, c["amount"]))
        elif c is None:
            est = price(code) * p["shares"]
            gone.append((est, p["name"], est))
        elif c["shares"] != p["shares"]:
            d = c["shares"] - p["shares"]
            est = price(code) * d
            (inc if d > 0 else dec).append((abs(est), c["name"], d, u, est))

    du = ((fc["units"] - fp["units"]) / fp["units"] * 100
          if fc["units"] and fp["units"] else 0)

    def row(left, right, cls):
        return (f'<div class="etfrow"><span>{left}</span>'
                f'<b class="{cls}">{right}</b></div>')

    body = [f'<div class="etffut">⚡ {h["name"]} {h["shares"]:,.0f}口'
            f'(佔淨值{h["weight"]:.1f}%)</div>'
            for h in curr.values() if "期貨" in (h["name"] or "")]
    for amt, name, shares, u, a in sorted(new, key=lambda x: -x[0]):
        body.append(row(f"🆕 {name} {shares:+,.0f}{u}",
                        ae._money(a) if a else "—", "up"))
    for est, name, e in sorted(gone, key=lambda x: -x[0]):
        body.append(row(f"❌ 清倉 {name}", ae._money(e), "dn"))
    for label, items, cls in (("➕ 加碼", inc, "up"), ("➖ 減碼", dec, "dn")):
        if not items:
            continue
        items.sort(key=lambda x: -x[0])
        body.append(f'<div class="etfsub">{label}</div>')
        for amt, name, d, u, est in items[:ae.TOP_N]:
            body.append(row(f"{name} {d:+,.0f}{u}", ae._money(est), cls))
        if len(items) > ae.TOP_N:
            body.append(f'<div class="etfflat">…另 {len(items) - ae.TOP_N} 筆</div>')
    if not (new or gone or inc or dec):
        body.append('<div class="etfflat">持股無變動</div>')

    return f"""
    <div class="card">
      <div class="hd">📊 {etf} {ae.FUNDS[etf]['name']}<small>{prev_d[5:]}→{curr_d[5:]} 申贖{du:+.1f}%</small></div>
      <div class="bd">{''.join(body)}</div>
    </div>"""


def render_etf(conn):
    """主動式 ETF 持股異動:7 檔各一張卡,多欄排版。回傳 (png path, 資料日) 或 None。"""
    from sources.active_etf import FUNDS
    cards = [h for etf in sorted(FUNDS) if (h := _sec_etf(conn, etf))]
    if not cards:
        return None
    dd = conn.execute("SELECT MAX(data_date) FROM fund_day").fetchone()[0]
    body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>{CSS}</style></head><body>
    <div class="banner">📊 主動式ETF持股異動<small>{dd[5:].replace('-', '/')} 資料</small></div>
    <div class="etfcol">{''.join(cards)}</div>
    </body></html>"""
    return _screenshot(body, "etf.png"), dd


# ================================================================ 個股籌碼
def _sec_stock_market(conn, label, table, dd):
    """一個市場(上市/上櫃)三欄:三大法人/外資/投信 買超前5、賣超前5(含連N標注)。"""
    from sources import inst_stock as st
    rows = [r for r in conn.execute(
        f"SELECT code, name, foreign_net, trust_net, total_net FROM {table} "
        "WHERE data_date=?", (dd,)) if st.STOCK_RE.match(r[0])]

    def line(r, idx, col, cls):
        code, name, net = r[0], r[1], r[idx]
        d = st._streak(conn, table, col, code, dd, net)
        tag = (f'<span class="chgsmall">連{d}{"買" if net > 0 else "賣"}</span>'
               if d >= 2 else "")
        return (f'<div class="etfrow"><span>{name}'
                f'<span class="muted">{code}</span></span>'
                f'<b class="{cls}">{net / 1000:+,.0f}張{tag}</b></div>')

    cols = []
    for title, idx, col in (("三大法人", 4, "total_net"),
                            ("外資", 2, "foreign_net"),
                            ("投信", 3, "trust_net")):
        ranked = sorted((r for r in rows if r[idx]), key=lambda r: -r[idx])
        buys = [r for r in ranked if r[idx] > 0][:5]
        sells = [r for r in ranked if r[idx] < 0][-5:][::-1]
        body = []
        if buys:
            body.append('<div class="etfsub"><span class="up">買超</span></div>')
            body += [line(r, idx, col, "up") for r in buys]
        if sells:
            body.append('<div class="etfsub"><span class="dn">賣超</span></div>')
            body += [line(r, idx, col, "dn") for r in sells]
        cols.append(f'<div class="col"><div class="colhd">{title}</div>'
                    f'{"".join(body) or "<div class=etfflat>—</div>"}</div>')
    return f"""
    <div class="card">
      <div class="hd">📊 {label}個股·法人買賣超<small>{dd[5:].replace('-', '/')}(張)</small></div>
      <div class="bd"><div class="cols">{''.join(cols)}</div></div>
    </div>"""


def _sec_stock_etf(conn, dd, odd):
    """主動ETF目前持股 ∩ 三大法人買賣超,取絕對值前 8。"""
    from sources import active_etf
    by_code = {r[0]: r for r in conn.execute(
        "SELECT code, name, total_net FROM inst_stock WHERE data_date=?", (dd,))}
    if odd:
        for r in conn.execute("SELECT code, name, total_net FROM inst_otc_stock "
                              "WHERE data_date=?", (odd,)):
            by_code.setdefault(r[0], r)
    watch = active_etf.latest_holding_codes(conn) & by_code.keys()
    if not watch:
        return ""
    top = sorted((by_code[c] for c in watch), key=lambda r: -abs(r[2]))[:8]
    rows = "".join(
        f'<div class="etfrow"><span>{name}<span class="muted">{code}</span></span>'
        f'<b class="{"up" if net > 0 else "dn"}">{net / 1000:+,.0f}張</b></div>'
        for code, name, net in top)
    return f"""
    <div class="card">
      <div class="hd">🎯 ETF關注股<small>主動ETF持股 ∩ 三大法人買賣超</small></div>
      <div class="bd"><div class="etfcol">{rows}</div></div>
    </div>"""


def render_stocks(conn):
    """個股籌碼:上市/上櫃各一卡(三大法人/外資/投信 買賣超前5)+ ETF關注股。"""
    dd = conn.execute("SELECT MAX(data_date) FROM inst_stock").fetchone()[0]
    if not dd:
        return None
    odd = conn.execute("SELECT MAX(data_date) FROM inst_otc_stock").fetchone()[0]
    cards = [f'<div class="row">{_sec_stock_market(conn, "上市", "inst_stock", dd)}</div>']
    if odd:
        cards.append(f'<div class="row">'
                     f'{_sec_stock_market(conn, "上櫃", "inst_otc_stock", odd)}</div>')
    etf = _sec_stock_etf(conn, dd, odd)
    if etf:
        cards.append(f'<div class="row">{etf}</div>')
    body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>{CSS}</style></head><body>
    <div class="banner">📊 個股籌碼<small>{dd[5:].replace('-', '/')} 三大法人買賣超前五</small></div>
    {''.join(cards)}
    </body></html>"""
    return _screenshot(body, "stocks.png"), dd


_RENDER = {"chips": render, "morning": render_morning,
           "chips_pre": render_chips_pre, "etf": render_etf,
           "stocks": render_stocks}


def deliver(conn, cfg, notifier, key):
    """render → 單獨 commit/push PNG → LINE 推圖。回傳是否成功推出圖片。"""
    got = _RENDER[key](conn)
    if not got:
        print(f"[dashboard] {key} 無資料可渲染,略過")
        return False
    png_path, dd = got
    base = Path(__file__).parent.parent
    rel = png_path.relative_to(base)
    subprocess.run(["git", "add", str(rel)], cwd=base, check=True,
                   capture_output=True)
    r = subprocess.run(["git", "commit", "-q", "-m", f"{png_path.stem} {dd}"],
                       cwd=base, capture_output=True)
    if r.returncode == 0:  # 有新內容才需要 push
        subprocess.run(["git", "push", "-q"], cwd=base, check=True,
                       capture_output=True, timeout=60)
    url = RAW_URL.format(dd=date.today().isoformat(), name=png_path.name)
    notifier.push_image(cfg, key, url)
    print(f"[dashboard] {key} 已推播圖片 {rel}")
    return True
