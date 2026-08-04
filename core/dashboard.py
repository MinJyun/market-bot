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
           "reports/{dd}/dashboard.png")
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
        ip = conn.execute("SELECT foreign_net FROM futures_inst "
                          "WHERE contract=? AND data_date=?",
                          (disp, idates[1] if len(idates) > 1 else "")).fetchone()
        if not lt and not ic:
            continue
        kv = []
        if ic:
            fchg = f"（前日 {_sgn(ic[0] - ip[0])}）" if ip else ""
            kv.append(f'<div class="kv"><span>外資</span><b>{_sgn(ic[0])} {fchg}</b></div>')
            kv.append(f'<div class="kv"><span>投信</span><b>{_sgn(ic[1])}</b></div>')
            kv.append(f'<div class="kv"><span>自營</span><b>{_sgn(ic[2])}</b></div>')
        if disp == "台指期" and cost:
            c, settle, pos, pnl, cstart = cost
            kv.append('<div class="sep"></div>')
            kv.append(f'<div class="kv"><span>外資成本(估)</span><b>{_n(c)}</b></div>')
            kv.append(f'<div class="kv"><span>結算</span><b>{_n(settle)}</b></div>')
            kv.append(f'<div class="kv"><span>{"浮盈" if pnl >= 0 else "浮虧"}</span>'
                      f'<b>{_sgn(pnl)} 億</b><span style="color:#889;font-size:12px">'
                      f'自{cstart[5:]}</span></div>')
        if lt:
            b5, b10, s5, s10, oi = lt
            n5, n10 = b5 - s5, b10 - s10
            c5 = f"（前日 {_sgn(n5 - (ltp[0] - ltp[2]))}）" if ltp else ""
            c10 = f"（前日 {_sgn(n10 - (ltp[1] - ltp[3]))}）" if ltp else ""
            kv.append('<div class="sep"></div>')
            kv.append(f'<div class="kv"><span>大戶前五大</span>'
                      f'<b>{"淨多" if n5 >= 0 else "淨空"} {abs(n5):,.0f} 口 {c5}</b></div>')
            kv.append(f'<div class="kv"><span>大戶前十大</span>'
                      f'<b>{"淨多" if n10 >= 0 else "淨空"} {abs(n10):,.0f} 口 {c10}</b></div>')
            oi_txt = f"未平倉 {oi:,.0f} 口"
        else:
            oi_txt = ""
        cols.append(f'<div class="col"><div class="colhd">{disp}</div>'
                    f'<div style="text-align:center;color:#667;font-size:13px">{oi_txt}</div>'
                    f'<div class="sep"></div>{"".join(kv)}</div>')
    return f"""
    <div class="card">
      <div class="hd">📊 期貨機構籌碼<small>{curr[5:].replace('-', '/')}(三大法人淨/口)</small></div>
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
    ip = conn.execute("SELECT foreign_net FROM options_inst WHERE data_date=?",
                      (ipd,)).fetchone() if ipd else None
    cpdd = conn.execute("SELECT MAX(data_date) FROM options_inst_cp").fetchone()[0]
    cps = {r[0]: r[1:] for r in conn.execute(
        "SELECT cp, foreign_net, trust_net, dealer_net FROM options_inst_cp "
        "WHERE data_date=?", (cpdd,))} if cpdd else {}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM options_lt ORDER BY data_date DESC LIMIT 2")]

    left = ['<div class="col" style="flex:0 0 330px">',
            '<div class="colhd">三大法人淨(不分買賣權)</div>']
    if ic:
        fchg = f"（前日 {_sgn(ic[0] - ip[0])}）" if ip else ""
        left.append(f'<div class="kv"><span>外資</span><b>{_sgn(ic[0])} {fchg}</b></div>')
        left.append(f'<div class="kv"><span>投信</span><b>{_sgn(ic[1])}</b></div>')
        left.append(f'<div class="kv"><span>自營</span><b>{_sgn(ic[2])}</b></div>')
    if cps:
        left.append('<div class="sep"></div><div style="text-align:center;'
                    'font-weight:700;font-size:14px;padding:2px 0">Call / Put 淨 OI</div>')
        for side, label in (("買權", "Call"), ("賣權", "Put")):
            if side in cps:
                fo, tr, de = cps[side]
                left.append(
                    f'<div class="kv"><span>{label}</span>'
                    f'<b style="font-size:13px">外資 {_sgn(fo)}｜'
                    f'投信 {_sgn(tr)}｜自營 {_sgn(de)}</b></div>')
    left.append("</div>")

    cols = ["".join(left)]
    for disp, tone in (("臺指Call", "#eef4ff"), ("臺指Put", "#fdeeee")):
        lt = conn.execute("SELECT buy5,buy10,sell5,sell10,oi FROM options_lt "
                          "WHERE contract=? AND data_date=?",
                          (disp, dates[0] if dates else "")).fetchone()
        if not lt:
            continue
        ltp = conn.execute("SELECT buy10,sell10 FROM options_lt "
                           "WHERE contract=? AND data_date=?",
                           (disp, dates[1] if len(dates) > 1 else "")).fetchone()
        b5, b10, s5, s10, oi = lt
        cb = f"（{_sgn(b10 - ltp[0])}）" if ltp else ""
        cs = f"（{_sgn(s10 - ltp[1])}）" if ltp else ""
        cols.append(f"""
        <div class="col" style="background:{tone}">
          <div class="colhd">{disp}</div>
          <div style="text-align:center;color:#667;font-size:13px">未平倉 {oi:,.0f} 口</div>
          <div class="sep"></div>
          <div class="kv"><span>前五大</span><b>買 {b5:,.0f}｜賣 {s5:,.0f}</b></div>
          <div class="kv"><span>前十大 買</span><b>{b10:,.0f} {cb}</b></div>
          <div class="kv"><span>前十大 賣</span><b>{s10:,.0f} {cs}</b></div>
        </div>""")
    return f"""
    <div class="card">
      <div class="hd">🧩 臺指選擇權機構籌碼<small>{idd[5:].replace('-', '/')}(口)</small></div>
      <div class="bd"><div class="cols">{''.join(cols)}</div>
      <div class="note">前五大/前十大為「所有契約」列之特定法人</div></div>
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


# ================================================================ 產圖與遞送
def render(conn):
    """組 HTML → Chrome 截圖 → 裁白邊,回傳 (png path, 資料日) 或 None。"""
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
    out_dir = store.REPORTS / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "dashboard.html"
    png_path = out_dir / "dashboard.png"
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
    return png_path, dd


def deliver(conn, cfg, notifier):
    """render → 單獨 commit/push PNG → LINE 推圖。失敗只印訊息,不影響文字。"""
    got = render(conn)
    if not got:
        print("[dashboard] 無資料可渲染,略過")
        return
    png_path, dd = got
    base = Path(__file__).parent.parent
    rel = png_path.relative_to(base)
    subprocess.run(["git", "add", str(rel)], cwd=base, check=True,
                   capture_output=True)
    r = subprocess.run(["git", "commit", "-q", "-m", f"dashboard {dd}"],
                       cwd=base, capture_output=True)
    if r.returncode == 0:  # 有新內容才需要 push
        subprocess.run(["git", "push", "-q"], cwd=base, check=True,
                       capture_output=True, timeout=60)
    url = RAW_URL.format(dd=date.today().isoformat())
    notifier.push_image(cfg, "chips", url)
    print(f"[dashboard] 已推播圖片 {rel}")
