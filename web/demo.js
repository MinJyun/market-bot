/** demo 分頁:某分點當日「相抵量」最大的幾檔個股 × 日內走勢 × 逐價位。
 *
 *  資料由 demo_build.py 產生(web/demo/index.json + 各股一檔),按需載入 ——
 *  10 檔全部 1.8 MB,一次載完會讓開頁面卡住,而使用者一次只看一檔。
 *
 *  **為什麼篩「相抵量」不篩淨額**:這個分點有大戶在打大量當沖單,買賣幾乎
 *  相抵,用淨額排序會把他排到最底下(2026-09-11 台達電進出 1,572 張、淨 +3 張)。
 *  相抵量 = min(買, 賣),配上金額門檻濾掉小型股。口徑細節見 demo_build.py。
 *
 *  設計重點是**三個面板共用同一條價格軸**:
 *    左:分點在各價位的買賣(橫向長條,買在右、賣在左)
 *    中:日內走勢(逐筆成交聚合)+ 均價線
 *    下:分時量能(內外盤分色)
 *  價格軸對齊之後,才看得出「價格在某個區間盤整時,這個分點在那邊做什麼」。
 *
 *  **做不到的那層**:原圖上那些「買 309.0 / 賣 308.0」的掛單軌跡來自即時
 *  五檔委託揭示 —— 那是盤中當下的狀態,撮合完就消失,沒有任何歷史資料集在賣。
 *  要有就得盤中自己錄。所以這裡只有「成交」,沒有「委託」。
 *
 *  另一個限制:逐筆成交**沒有分點身分**,分點資料**沒有時間**。兩者唯一的
 *  交集是價格,所以只能用價格軸並排,不能真的把「幾點是誰買的」連起來。
 */
import {$, fmt, cls, numf, showTip, hideTip} from "./lib.js";

/** ⚠ 單位不一致(實測 2026-09-11 信昌電):
 *    TaiwanStockPriceTick 的 volume      → **張**(逐筆合計 59,199)
 *    TaiwanStockPrice 的 Trading_Volume  → **股**(59,856,602)
 *    TaiwanStockTradingDailyReport 的 buy/sell → **股**
 *  所以 tick 的量直接用,另外兩個要 ÷1000 才是張。兩者差 1,011 倍而不是
 *  正好 1,000 倍 —— 逐筆不含盤後定價與零股,這個差距是正常的。 */
let D = null;
const T0 = 9 * 3600, T1 = 13.5 * 3600;          // 09:00 ~ 13:30

/** 逐筆 → 等距時間桶。回傳 [{t, o,h,l,c, vol, out, in}]。 */
function bucketize(ticks, sec) {
  const out = [];
  let cur = null;
  for (const [t, p, v, tt] of ticks) {
    const k = Math.floor((t - T0) / sec);
    if (!cur || cur.k !== k) {
      cur = {k, t: T0 + k * sec, o: p, h: p, l: p, c: p, vol: 0, out: 0, in: 0};
      out.push(cur);
    }
    cur.h = Math.max(cur.h, p); cur.l = Math.min(cur.l, p); cur.c = p;
    cur.vol += v;
    if (tt === 2) cur.out += v; else if (tt === 1) cur.in += v;
  }
  return out;
}

const hhmm = t => `${String(Math.floor(t / 3600)).padStart(2, "0")}:`
                + `${String(Math.floor(t / 60) % 60).padStart(2, "0")}`;

/** 走勢 + 逐價位,**畫在同一張 SVG**。
 *
 *  原本拆成兩個 SVG 並排,價格軸對不齊 —— 兩者 viewBox 長寬比不同
 *  (330×424 vs 760×424),而 `width:100%` 的實際渲染高度 = 容器寬 × H/W,
 *  所以同一個價格在左右兩張圖落在不同的 y。合併成一張就天生共用 y 軸,
 *  水平指示線也才能真的貫穿左右。
 */
function draw(el, bars, rows, market, lo, hi, avgLine) {
  const W = 1180, HP = 380, GAP = 12, HV = 120, H = HP + GAP + HV + 22;
  const LAD = {x: 4, w: 300};                       // 左:逐價位
  const FLO = {x: 336, w: W - 336 - 56};            // 右:走勢(右側留給價格刻度)
  const TOP = 12;
  const y = v => HP - ((v - lo) / ((hi - lo) || 1)) * (HP - TOP);
  const n = bars.length;
  const xf = i => FLO.x + FLO.w * (i + 0.5) / n;
  const bw = Math.max(0.6, Math.min(6, FLO.w / n * 0.8));
  const mid = LAD.x + LAD.w / 2;
  const mx = Math.max(...rows.map(r => Math.max(r.b, r.s)), 1);
  const half = LAD.w / 2 - 6;
  const mkt = {}; market.forEach(m => mkt[m.p] = m);
  // 長條高度 = 一個檔位在價格軸上的高度。**檔位不能寫死 0.5** —— 台股的
  // 升降單位隨價格變(100~500 元是 0.5,500~1000 是 1,1000 元以上是 5),
  // 原本固定 0.5 的寫法讓台達電(1600 元、檔位 5 元)的 10 個價位變成 4px
  // 細條散在 41px 的間隔裡,看起來像資料很稀疏,其實是每一檔都有量。
  // 取實際相鄰價位的最小間距,不查表:資料自己就說了它的檔位是多少。
  const gaps = rows.slice(1).map((r, i) => rows[i].p - r.p).filter(v => v > 0);
  const step = gaps.length ? Math.min(...gaps) : 0.5;
  const bh = Math.max(1.5, (HP - TOP) * step / ((hi - lo) || 1) - 0.4);
  const vMax = Math.max(...bars.map(b => b.vol), 1);
  const vTop = HP + GAP, vBot = vTop + HV;
  const yV = v => vBot - (v / vMax) * HV;

  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  // 價格格線:橫貫左右兩區,本身就是對齊的視覺證據
  for (let i = 0; i <= 5; i++) {
    const v = lo + (hi - lo) * i / 5, yy = y(v);
    s += `<line x1="${LAD.x}" y1="${yy}" x2="${FLO.x + FLO.w}" y2="${yy}"`
       + ` stroke="var(--line)" stroke-dasharray="2 5" opacity=".45"/>`
       + `<text x="${FLO.x + FLO.w + 5}" y="${yy + 3}" font-size="10"`
       + ` fill="var(--dim)">${v.toFixed(1)}</text>`;
  }
  // ---- 左:逐價位橫條(賣在左、買在右)
  s += `<line x1="${mid}" y1="${TOP}" x2="${mid}" y2="${HP}"`
     + ` stroke="var(--line)"/>`;
  rows.forEach(r => {
    const yy = y(r.p) - bh / 2;
    if (r.s) s += `<rect x="${(mid - r.s / mx * half).toFixed(1)}"`
      + ` y="${yy.toFixed(1)}" width="${(r.s / mx * half).toFixed(1)}"`
      + ` height="${bh.toFixed(1)}" fill="var(--dn)" opacity=".85"/>`;
    if (r.b) s += `<rect x="${mid}" y="${yy.toFixed(1)}"`
      + ` width="${(r.b / mx * half).toFixed(1)}" height="${bh.toFixed(1)}"`
      + ` fill="var(--up)" opacity=".85"/>`;
  });
  s += `<text x="${LAD.x}" y="${HP + 14}" font-size="10" fill="var(--dn)">← 賣</text>`
     + `<text x="${LAD.x + LAD.w}" y="${HP + 14}" font-size="10" fill="var(--up)"`
     + ` text-anchor="end">買 →</text>`;
  // ---- 右:成交價 + 當日累計均價
  s += `<polyline fill="none" stroke="#e8d44d" stroke-width="1.2"`
     + ` points="${bars.map((b, i) => `${xf(i)},${y(b.c)}`).join(" ")}"/>`;
  if (avgLine.length)
    s += `<polyline fill="none" stroke="#fff" stroke-width="1" opacity=".75"`
       + ` points="${avgLine.map((v, i) => `${xf(i)},${y(v)}`).join(" ")}"/>`;
  // ---- 量能(外盤在下、內盤疊上去)
  s += `<line x1="${FLO.x}" y1="${vBot}" x2="${FLO.x + FLO.w}" y2="${vBot}"`
     + ` stroke="var(--line)"/>`;
  bars.forEach((b, i) => {
    const hOut = vBot - yV(b.out), hIn = vBot - yV(b.in);
    s += `<rect x="${(xf(i) - bw / 2).toFixed(1)}" y="${(vBot - hOut).toFixed(1)}"`
       + ` width="${bw.toFixed(1)}" height="${hOut.toFixed(1)}"`
       + ` fill="var(--up)" opacity=".85"/>`
       + `<rect x="${(xf(i) - bw / 2).toFixed(1)}"`
       + ` y="${(vBot - hOut - hIn).toFixed(1)}" width="${bw.toFixed(1)}"`
       + ` height="${hIn.toFixed(1)}" fill="var(--dn)" opacity=".85"/>`;
  });
  s += `<text x="${FLO.x + FLO.w + 5}" y="${vTop + 10}" font-size="10"`
     + ` fill="var(--dim)">${numf(vMax)} 張</text>`;
  bars.forEach((b, i) => {
    if (Math.floor(b.t / 3600) !== Math.floor((bars[i - 1] || {t: 0}).t / 3600))
      s += `<text x="${xf(i)}" y="${H - 6}" font-size="10" fill="var(--dim)"`
         + ` text-anchor="middle">${hhmm(b.t)}</text>`;
  });
  // ---- 指示線:水平(價格,貫穿左右)+ 垂直(時間)
  s += `<line class="hline" x1="${LAD.x}" y1="0" x2="${FLO.x + FLO.w}" y2="0"`
     + ` stroke="#e8d44d" stroke-width="1" stroke-dasharray="4 3" opacity="0"`
     + ` pointer-events="none"/>`
     + `<line class="vline" x1="0" y1="${TOP}" x2="0" y2="${vBot}"`
     + ` stroke="var(--dim)" stroke-width="1" stroke-dasharray="3 3"`
     + ` opacity="0" pointer-events="none"/>`
     + `<text class="plab" x="0" y="0" font-size="10" fill="#e8d44d"`
     + ` opacity="0" pointer-events="none"></text>`;
  // 熱區:右邊逐桶(時間)、左邊逐價位(價格)
  bars.forEach((b, i) => {
    s += `<rect class="hitT" x="${(xf(i) - Math.max(bw, 5) / 2).toFixed(1)}"`
       + ` y="0" width="${Math.max(bw, 5).toFixed(1)}" height="${vBot}"`
       + ` fill="transparent" data-i="${i}"/>`;
  });
  rows.forEach(r => {
    s += `<rect class="hitP" x="${LAD.x}" y="${(y(r.p) - bh / 2).toFixed(1)}"`
       + ` width="${LAD.w}" height="${Math.max(bh, 3).toFixed(1)}"`
       + ` fill="transparent" data-p="${r.p}"/>`;
  });
  el.innerHTML = s + "</svg>";

  const hl = el.querySelector("line.hline"), vl = el.querySelector("line.vline");
  const pl = el.querySelector("text.plab");
  const setH = (price) => {
    hl.setAttribute("y1", y(price)); hl.setAttribute("y2", y(price));
    hl.setAttribute("opacity", ".8");
    pl.setAttribute("x", mid); pl.setAttribute("y", y(price) - 3);
    pl.setAttribute("text-anchor", "middle");
    pl.setAttribute("opacity", "1");
    pl.textContent = price.toFixed(1);
  };
  const hide = () => {
    hl.setAttribute("opacity", 0); vl.setAttribute("opacity", 0);
    pl.setAttribute("opacity", 0); hideTip();
  };
  // 滑過走勢 → 垂直線 + 水平線落在該時點的成交價,左圖同價位一起被標示
  el.querySelectorAll("rect.hitT").forEach(h => {
    h.onmousemove = ev => {
      const i = +h.dataset.i, b = bars[i];
      vl.setAttribute("x1", xf(i)); vl.setAttribute("x2", xf(i));
      vl.setAttribute("opacity", ".75");
      setH(b.c);
      const at = rows.find(r => Math.abs(r.p - b.c) < 0.001);
      showTip(`<b>${hhmm(b.t)}</b>　收 <b>${b.c}</b>`
        + `<br>開 ${b.o} 高 ${b.h} 低 ${b.l}　量 ${numf(b.vol)} 張`
        + `<br>外盤 <b class="up">${numf(b.out)}</b>`
        + ` / 內盤 <b class="dn">${numf(b.in)}</b>`
        + ` <span class="${b.out - b.in >= 0 ? 'up' : 'dn'}">(${
          fmt(b.out - b.in, 0)})</span>`
        + `<hr style="border:0;border-top:1px solid var(--line);margin:4px 0">`
        + (at ? `<span class="dim">${D.broker_name}在 ${b.c} 元:</span> `
              + `買 <b class="up">${numf(at.b / 1000)}</b>`
              + ` / 賣 <b class="dn">${numf(at.s / 1000)}</b> 張`
              : `<span class="dim">${D.broker_name}在 ${b.c} 元無進出</span>`), ev);
    };
    h.onmouseleave = hide;
  });
  // 滑過逐價位 → 水平線貫穿到走勢圖,看得出該價位當天被觸及幾次
  el.querySelectorAll("rect.hitP").forEach(h => {
    h.onmousemove = ev => {
      const p = +h.dataset.p, r = rows.find(x => x.p === p);
      const m = mkt[p] || {b: 0, s: 0};
      vl.setAttribute("opacity", 0);
      setH(p);
      const touched = bars.filter(b => b.l <= p && p <= b.h);
      const first = touched[0], last = touched[touched.length - 1];
      showTip(`<b>${p.toFixed(1)} 元</b>`
        + `<br>${D.broker_name} 買 <b class="up">${numf(r.b / 1000)}</b>`
        + ` / 賣 <b class="dn">${numf(r.s / 1000)}</b> 張`
        + ` <span class="${r.b - r.s >= 0 ? 'up' : 'dn'}">(${
          fmt((r.b - r.s) / 1000, 0)})</span>`
        + `<br><span class="dim">全市場 ${numf(m.b / 1000)} 張,${D.broker_name}佔 ${
          m.b ? (r.b / m.b * 100).toFixed(0) : 0}%</span>`
        + (touched.length
            ? `<hr style="border:0;border-top:1px solid var(--line);margin:4px 0">`
              + `<span class="dim">此價位當天被觸及 ${touched.length} 次,`
              + `${hhmm(first.t)} ~ ${hhmm(last.t)}</span>`
            : ""), ev);
    };
    h.onmouseleave = hide;
  });
}

let IDX = null;                     // demo/index.json
const CACHE = {};                   // {stock_id: 該股的完整資料}

const yi = v => (v / 1e8).toFixed(2);        // 元 → 億
const wan = v => Math.round(v / 1e4);        // 元 → 萬

/** 挑選列:每檔一張卡,顯示決定它入選的那三個數字。 */
function renderPicks(on) {
  $("#demo-picks").innerHTML = IDX.stocks.map(s => `
    <button data-sid="${s.stock_id}" aria-pressed="${s.stock_id === on}">
      <div class="nm">${s.stock_id} ${s.name}</div>
      <div class="sub">相抵 ${yi(s.dt_amount)} 億 · 佔量 ${s.dt_pct}%</div>
      <div class="sub">對稱 ${s.asym_pct}% · <span class="${cls(s.dt_pnl)}">${
        fmt(wan(s.dt_pnl), 0)} 萬</span></div>
    </button>`).join("");
  $("#demo-picks").querySelectorAll("button[data-sid]").forEach(b => {
    b.onclick = () => loadStock(b.dataset.sid);
  });
}

/** 換檔:先讀快取,沒有才抓。抓的期間標題先換掉,不然點了像沒反應。 */
async function loadStock(sid) {
  renderPicks(sid);
  if (!CACHE[sid]) {
    $("#demo-title").textContent = `${sid} 載入中…`;
    CACHE[sid] = await (await fetch(`demo/${sid}.json`)).json();
  }
  D = CACHE[sid];
  render();
}

export async function initDemo() {
  if (IDX) return;
  IDX = await (await fetch("demo/index.json")).json();
  const p = IDX.params;
  $("#demo-head").textContent =
    `${IDX.broker_name}（${IDX.bno}）${IDX.date}　相抵量最大的 ${
      IDX.stocks.length} 檔`;
  $("#demo-rule").innerHTML =
    `<b>相抵量</b> = min(當日買股數, 賣股數)，即這個分點同日買進又賣出、`
    + `互相抵消掉的部分。篩選條件:相抵金額 ≥ <b>${p.min_amount} 億</b>、`
    + `相抵量佔該股日線成交量 ≥ <b>${p.min_pct}%</b>、`
    + `對稱度 |買−賣|÷相抵量 < <b>${p.max_asym}%</b>（越小越純粹是沖，`
    + `不是邊沖邊建倉）。<br>`
    + `<b>為什麼不用淨額排序</b>:當沖買賣相抵，淨額趨近 0 —— `
    + `台達電那天元大進出 1,572 張、淨額只有 +3 張，用淨額排會排到最底下。<br>`
    + `<b>口徑但書</b>:分點資料看不出是同一個客戶一買一賣，`
    + `相抵量也可能是兩個客戶各做一邊，<b>不等於證交所定義的當沖</b>；`
    + `這是分點資料能逼近的極限。分母一律用日線成交量，不用分點合計`
    + `（分點成交量系統性少於官方成交量）。`;
  await loadStock(IDX.stocks[0].stock_id);
}

function render() {
  const q = D.quote || {}, m = D.metrics || {};
  $("#demo-title").textContent =
    `${D.name}（${D.stock_id}）× ${D.broker_name}（${D.bno}）　${D.date}`;
  $("#demo-kpi").innerHTML =
    `<div><div class="k">開</div><div class="v">${q.open}</div></div>
     <div><div class="k">高</div><div class="v up">${q.high}</div></div>
     <div><div class="k">低</div><div class="v dn">${q.low}</div></div>
     <div><div class="k">收</div><div class="v">${q.close} <span class="${
       cls(q.spread)}">${fmt(q.spread, 2)}</span></div></div>
     <div><div class="k">成交量</div><div class="v">${
       numf(q.volume / 1000)} 張</div></div>
     <div><div class="k">相抵量</div><div class="v">${
       numf(m.dt_sh / 1000)} 張</div></div>
     <div><div class="k">相抵金額</div><div class="v">${
       yi(m.dt_amount)} 億</div></div>
     <div><div class="k">佔日線量</div><div class="v">${m.dt_pct}%</div></div>
     <div><div class="k">淨額</div><div class="v ${cls(m.net_sh)}">${
       fmt(m.net_sh / 1000, 0)} 張</div></div>
     <div><div class="k">相抵損益</div><div class="v ${cls(m.dt_pnl)}">${
       fmt(wan(m.dt_pnl), 0)} 萬</div></div>
     <div><div class="k">買/賣均價</div><div class="v">${
       (m.buy_vwap || 0).toFixed(2)} / ${(m.sell_vwap || 0).toFixed(2)}</div></div>
     <div><div class="k">逐筆成交</div><div class="v">${
       numf(D.ticks.length)} 筆</div></div>`;

  const rebuild = () => {
    const sec = +$("#demo-bucket").value;
    const bars = bucketize(D.ticks, sec);
    // 價格軸範圍取「走勢 + 該分點有進出的價位」的聯集,兩張圖才能真的對齊
    const ps = D.broker_price.map(r => r.p);
    const lo = Math.min(q.low, ...ps), hi = Math.max(q.high, ...ps);
    // 當日累計 VWAP(逐桶推進),不是各桶自己的均價
    let cv = 0, ca = 0;
    const avg = bars.map(b => { cv += b.vol; ca += b.c * b.vol; return ca / cv; });
    draw($("#demo-chart"), bars, D.broker_price, D.market_price, lo, hi, avg);
    $("#demo-bucket-note").textContent =
      `${bars.length} 根（每根 ${sec < 60 ? sec + " 秒" : sec / 60 + " 分"}）`;
  };
  $("#demo-bucket").onchange = rebuild;
  rebuild();

  // 下方明細表:逐價位的原始數字
  const tb = D.broker_price;
  const sum = (k) => tb.reduce((s, r) => s + r[k], 0);
  $("#demo-table").innerHTML =
    `<table><tr><th>價位</th><th class="num">買(張)</th><th class="num">賣(張)</th>`
    + `<th class="num">淨(張)</th><th class="num">全市場買(張)</th>`
    + `<th class="num">${D.broker_name}佔比</th></tr>`
    // 內層變數不叫 m —— 外面的 m 是 D.metrics,同名會被遮住
    + tb.map(r => {
        const mk = D.market_price.find(x => x.p === r.p) || {b: 0};
        // `|| 0` 是為了把 -0 變成 0:零股列(例如只賣 118 股)四捨五入後是
        // -0,fmt 會印出「-0」看起來像錯誤。-0 是 falsy,所以這樣就夠。
        const n = Math.round((r.b - r.s) / 1000) || 0;
        return `<tr><td>${r.p.toFixed(1)}</td>`
          + `<td class="num up">${numf(r.b / 1000)}</td>`
          + `<td class="num dn">${numf(r.s / 1000)}</td>`
          + `<td class="num ${cls(n)}">${fmt(n, 0)}</td>`
          + `<td class="num dim">${numf(mk.b / 1000)}</td>`
          + `<td class="num dim">${mk.b ? (r.b / mk.b * 100).toFixed(0) + "%" : "—"}</td></tr>`;
      }).join("")
    + `<tr><td><b>合計</b></td><td class="num up"><b>${numf(sum("b") / 1000)}</b></td>`
    + `<td class="num dn"><b>${numf(sum("s") / 1000)}</b></td>`
    + `<td class="num ${cls(sum("b") - sum("s"))}"><b>${
        fmt((sum("b") - sum("s")) / 1000, 0)}</b></td>`
    + `<td class="num dim">${numf(q.volume / 1000)}</td>`
    + `<td class="num dim">${(sum("b") / q.volume * 100).toFixed(1)}%</td></tr>`
    // 分點與日線同為「股」,故佔比直接相除;不要拿 tick 的張數當分母
    + `</table>`;
}
