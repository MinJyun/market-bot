/** 圖表(inline SVG,無外部依賴)。
 *
 *  原本 5 個圖表函式各自重寫座標軸骨架:viewBox 出現 6 次、padding 定義 5 次、
 *  熱區 3 次、十字線 2 次、tooltip 3 次。後果是每加一張圖就複製一遍,而且
 *  十字線只加在 K 線那張、大盤那張得再寫一次。共用部分抽成 axes()。
 */
import {fmt, dstr, showTip, hideTip} from "./lib.js";

/** 座標軸骨架。回傳一組小工具,各圖只負責畫自己的 marks。 */
export function axes(n, opt = {}) {
  const W = opt.W ?? 1100, H = opt.H ?? 300;
  const P = {t: 14, r: 56, b: 26, l: 56, ...(opt.P || {})};
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  // 柱狀圖用「槽位中心」(i+0.5)/n;純折線圖用 spread:true 讓端點貼齊左右緣
  // (chartFund 原本就是這樣算的,兩者混用會讓十字線與線對不上,故做成選項)
  const x = opt.spread
    ? (i => n < 2 ? P.l + iw / 2 : P.l + i / (n - 1) * iw)
    : (i => P.l + iw * (i + 0.5) / n);
  const bw = Math.max(opt.minBw ?? 1,
                      Math.min(opt.maxBw ?? 1e9, iw / n * (opt.bwFrac ?? 0.72)));
  return {
    W, H, P, iw, ih, n, x, bw,
    /** 線性對映:值域 [lo,hi] → 畫布 [bot,top](y 軸向下,故 bot 在數值低端) */
    scale: (lo, hi, top, bot) => {
      const sp = (hi - lo) || 1;
      return v => bot - ((v - lo) / sp) * (bot - top);
    },
    open: () => `<svg viewBox="0 0 ${W} ${H}">`,
    close: () => "</svg>",
    /** 零軸橫線 */
    zero: y => `<line x1="${P.l}" y1="${y}" x2="${P.l + iw}" y2="${y}"`
               + ` stroke="var(--line)"/>`,
    /** 左/右軸的文字標註 */
    lab: (side, y, txt, color = "var(--dim)", size = 10) =>
      `<text x="${side === "l" ? 4 : W - P.r + 4}" y="${y}" fill="${color}"`
      + ` font-size="${size}">${txt}</text>`,
    /** X 軸日期標籤:最多 12 個,免得擠成一團 */
    xLabels: (dates, y = H - 8) => {
      const step = Math.max(1, Math.ceil(n / 12));
      return dates.map((d, i) => i % step === 0
        ? `<text x="${x(i)}" y="${y}" fill="var(--dim)" font-size="10"`
          + ` text-anchor="middle">${dstr(d)}</text>` : "").join("");
    },
    /** 十字線。畫在熱區之前 + pointer-events:none,才不會擋住滑鼠事件 */
    cross: (y0 = P.t, y1 = H - 18) =>
      `<line class="cross" x1="0" y1="${y0}" x2="0" y2="${y1}"`
      + ` stroke="var(--dim)" stroke-width="1" stroke-dasharray="3 3"`
      + ` opacity="0" pointer-events="none"/>`,
    /** 逐日全高透明熱區:細柱也點得到 */
    hits: (extra = () => "") => {
      let s = "";
      for (let i = 0; i < n; i++)
        s += `<rect class="hit" x="${x(i) - Math.max(bw, 6) / 2}" y="0"`
           + ` width="${Math.max(bw, 6)}" height="${H}" fill="transparent"`
           + ` data-i="${i}"${extra(i)}/>`;
      return s;
    },
    /** 綁 hover:移動十字線 + 顯示 tooltip;可另外綁點擊 */
    bind: (el, tip, onClick) => {
      const cross = el.querySelector("line.cross");
      el.querySelectorAll("rect.hit").forEach(h => {
        const i = +h.dataset.i;
        h.onmousemove = ev => {
          if (cross) {
            cross.setAttribute("x1", x(i));
            cross.setAttribute("x2", x(i));
            cross.setAttribute("opacity", ".75");
          }
          if (tip) showTip(tip(i), ev);
        };
        h.onmouseleave = () => {
          if (cross) cross.setAttribute("opacity", 0);
          hideTip();
        };
        if (onClick) { h.style.cursor = "pointer"; h.onclick = () => onClick(i); }
      });
    },
  };
}

const empty = (el, msg = "此區間無資料") =>
  void (el.innerHTML = `<div class="spin">${msg}</div>`);

/** 每日淨額柱 + 累積線;withPrice 時另疊一條獨立縮放的收盤價線。 */
export function chartFlow(el, daily, onPick, withPrice) {
  if (!daily.length) return empty(el);
  const a = axes(daily.length, {maxBw: 1e9});
  const nets = daily.map(x => x.net);
  let c = 0; const cums = nets.map(v => c += v);
  const nMax = Math.max(...nets.map(Math.abs)) || 1;
  const cMin = Math.min(0, ...cums), cMax = Math.max(0, ...cums);
  const mid = a.P.t + a.ih / 2;
  const yBar = a.scale(-nMax, nMax, a.P.t + 6, a.P.t + a.ih - 6);
  const yCum = a.scale(cMin, cMax, a.P.t, a.P.t + a.ih);
  let s = a.open() + a.zero(mid);
  daily.forEach((d, i) => {
    const y1 = yBar(d.net);
    s += `<rect x="${a.x(i) - a.bw / 2}" y="${Math.min(mid, y1)}"`
       + ` width="${a.bw}" height="${Math.abs(y1 - mid) || 0.6}" rx="1"`
       + ` pointer-events="none" fill="${d.net >= 0 ? 'var(--up)' : 'var(--dn)'}"`
       + ` opacity=".78"/>`;
  });
  s += `<polyline fill="none" stroke="var(--accent)" stroke-width="1.8"`
     + ` points="${cums.map((v, i) => `${a.x(i)},${yCum(v)}`).join(" ")}"/>`;
  s += a.lab("l", a.P.t + 10, `日淨額 ${nMax.toFixed(0)}億`, "var(--dim)", 11);
  s += a.lab("r", yCum(cums[a.n - 1]) + 4, `累積 ${fmt(cums[a.n - 1], 0)}億`,
             "var(--accent)", 11);
  if (withPrice) {
    const ps = daily.map(x => x.close).filter(v => v != null);
    if (ps.length > 1) {
      const lo = Math.min(...ps), hi = Math.max(...ps);
      const yP = a.scale(lo, hi, a.P.t + a.ih * 0.05, a.P.t + a.ih * 0.95);
      const seq = daily.map((d, i) => d.close != null
        ? `${a.x(i)},${yP(d.close)}` : null).filter(Boolean);
      s += `<polyline fill="none" stroke="#f59e0b" stroke-width="1.4"`
         + ` opacity=".85" points="${seq.join(" ")}"/>`;
      s += a.lab("l", a.P.t + a.ih - 2, `收盤 ${lo.toFixed(0)}~${hi.toFixed(0)}`,
                 "#f59e0b");
    }
  }
  s += a.xLabels(daily.map(d => d.d));
  s += a.cross() + a.hits();
  el.innerHTML = s + a.close();
  a.bind(el, i => {
    const d = daily[i];
    return `<b>${dstr(d.d)}</b><br>淨 <b class="${
      d.net >= 0 ? 'up' : 'dn'}">${fmt(d.net, 2)} 億</b>`
      + `<br>買 ${d.buy.toFixed(1)} / 賣 ${d.sell.toFixed(1)} 億`
      + (onPick ? "<br><span style='opacity:.7'>點擊看當日明細</span>" : "");
  }, onPick ? i => onPick(daily[i].d) : null);
}

/** 上:K 線(有價格才畫)+ 買賣均價虛線;下:每日淨額柱 + 累積線。 */
export function chartCandle(el, rows, vwap) {
  if (!rows.length) return empty(el);
  const HK = 260, GAP = 18, HF = 170;
  const a = axes(rows.length, {H: HK + GAP + HF, P: {l: 56, r: 58, t: 12, b: 24},
                               bwFrac: 0.62, minBw: 1.2, maxBw: 9});
  const hasPx = rows.some(r => r.close != null);
  let yK = () => 0, lo = 0, hi = 0;
  if (hasPx) {
    hi = Math.max(...rows.filter(r => r.high != null).map(r => r.high));
    lo = Math.min(...rows.filter(r => r.low != null).map(r => r.low));
    [vwap?.buy, vwap?.sell].forEach(v => {
      if (v) { hi = Math.max(hi, v); lo = Math.min(lo, v); } });
    yK = a.scale(lo, hi, a.P.t + 4, HK - 4);
  }
  const nets = rows.map(r => r.net || 0);
  const nMax = Math.max(...nets.map(Math.abs)) || 1;
  const fTop = HK + GAP, fMid = fTop + (HF - a.P.b) / 2;
  const yBar = a.scale(-nMax, nMax, fTop + 4, fTop + HF - a.P.b - 4);
  let c = 0; const cums = nets.map(v => c += v);
  const cMin = Math.min(0, ...cums), cMax = Math.max(0, ...cums);
  const yCum = a.scale(cMin, cMax, fTop, fTop + HF - a.P.b);
  let s = a.open();
  if (hasPx) {
    [["buy", "up", "買均"], ["sell", "dn", "賣均"]].forEach(([k, col, lbl]) => {
      const v = vwap && vwap[k];
      if (!v) return;
      s += `<line x1="${a.P.l}" y1="${yK(v)}" x2="${a.P.l + a.iw}" y2="${yK(v)}"`
         + ` stroke="var(--${col})" stroke-width="1" stroke-dasharray="4 3"`
         + ` opacity=".7"/>`
         + a.lab("r", yK(v) + 4, `${lbl} ${v.toFixed(1)}`, `var(--${col})`);
    });
    rows.forEach((r, i) => {
      if (r.close == null) return;
      const col = r.close >= r.open ? "var(--up)" : "var(--dn)";
      const yo = yK(r.open), yc = yK(r.close);
      s += `<line x1="${a.x(i)}" y1="${yK(r.high)}" x2="${a.x(i)}"`
         + ` y2="${yK(r.low)}" stroke="${col}" stroke-width="1"/>`
         + `<rect x="${a.x(i) - a.bw / 2}" y="${Math.min(yo, yc)}"`
         + ` width="${a.bw}" height="${Math.max(1, Math.abs(yc - yo))}"`
         + ` fill="${col}"/>`;
    });
    s += a.lab("l", a.P.t + 8, hi.toFixed(0)) + a.lab("l", HK - 2, lo.toFixed(0));
  }
  s += a.zero(fMid);
  nets.forEach((v, i) => {
    const y1 = yBar(v);
    s += `<rect x="${a.x(i) - a.bw / 2}" y="${Math.min(fMid, y1)}"`
       + ` width="${a.bw}" height="${Math.abs(y1 - fMid) || 0.6}"`
       + ` fill="${v >= 0 ? 'var(--up)' : 'var(--dn)'}" opacity=".8"/>`;
  });
  s += `<polyline fill="none" stroke="var(--accent)" stroke-width="1.5"`
     + ` points="${cums.map((v, i) => `${a.x(i)},${yCum(v)}`).join(" ")}"/>`;
  s += a.lab("l", fTop + 10, `日淨 ${nMax.toFixed(0)}億`);
  s += a.lab("r", yCum(cums[a.n - 1]) + 4, `累積 ${fmt(cums[a.n - 1], 0)}億`,
             "var(--accent)");
  s += a.xLabels(rows.map(r => r.d), a.H - 6);
  s += a.cross() + a.hits();
  el.innerHTML = s + a.close();
  a.bind(el, i => {
    const r = rows[i];
    const px = r.close == null ? "" :
      `開 ${r.open} 高 ${r.high} 低 ${r.low} <b>收 ${r.close}</b>`
      + ` <span class="${r.close >= r.open ? 'up' : 'dn'}">${
        fmt(r.close - r.open, 2)}</span><br>量 ${
        ((r.vol || 0) / 1000).toLocaleString()} 張<br>`;
    return `<b>${r.d}</b><br>${px}`
      + `買 ${(r.buy_sh / 1000).toLocaleString(undefined, {maximumFractionDigits: 0})} 張`
      + ` / 賣 ${(r.sell_sh / 1000).toLocaleString(undefined, {maximumFractionDigits: 0})} 張`
      + `<br>淨 <b class="${r.net >= 0 ? 'up' : 'dn'}">${fmt(r.net, 2)} 億</b>`;
  });
}

/** 重點分點每日淨額:正的往上疊、負的往下疊 + 累積線(右軸)。
 *
 *  原本每個分點各畫一條折線 —— 13 條互相纏繞,看得出單一分點的起伏,卻看不出
 *  「這群分點今天整體站哪邊」,而後者才是要看的。
 *  **色塊高度不設最小值**:堆疊圖的意義是「柱總高 = 當日淨額總和」,補最小
 *  高度會把總高撐大(實測 156 個色塊有 52 個會被撐),那等於騙人。 */
export function chartStack(el, rows, nameOf = b => b) {
  if (!rows.length) return empty(el);
  const dates = [...new Set(rows.map(r => r.d))].sort((x, y) => x - y);
  const keys = [...new Set(rows.map(r => r.bno))].sort();
  const byD = {}; rows.forEach(r => (byD[r.d] = byD[r.d] || []).push(r));
  const a = axes(dates.length, {P: {t: 14, r: 58, b: 26, l: 56},
                                bwFrac: 0.7, minBw: 1.5, maxBw: 14});
  const tot = {}; let mx = 0;
  dates.forEach(d => {
    const rs = byD[d] || [];
    const up = rs.reduce((s, r) => s + (r.net > 0 ? r.net : 0), 0);
    const dn = rs.reduce((s, r) => s + (r.net < 0 ? r.net : 0), 0);
    tot[d] = up + dn; mx = Math.max(mx, up, -dn);
  });
  mx = mx || 1;
  const mid = a.P.t + a.ih / 2;
  const y = a.scale(-mx, mx, a.P.t + 6, a.P.t + a.ih - 6);
  const hues = [0, 30, 60, 120, 180, 210, 260, 300, 340, 20, 90, 160, 240];
  const col = b => `hsl(${hues[keys.indexOf(b) % hues.length]} 65% 48%)`;
  let s = a.open() + a.zero(mid);
  dates.forEach((d, i) => {
    // 固定用 keys 的順序堆疊,否則同一個分點每天的色塊位置會跳動
    const rs = (byD[d] || []).slice()
      .sort((p, q) => keys.indexOf(p.bno) - keys.indexOf(q.bno));
    let up = 0, dn = 0;
    rs.forEach(r => {
      if (!r.net) return;
      const from = r.net > 0 ? up : dn, to = from + r.net;
      if (r.net > 0) up = to; else dn = to;
      const yA = y(from), yB = y(to);
      s += `<rect x="${(a.x(i) - a.bw / 2).toFixed(1)}"`
         + ` y="${Math.min(yA, yB).toFixed(2)}" width="${a.bw.toFixed(1)}"`
         + ` height="${Math.abs(yB - yA).toFixed(2)}" fill="${col(r.bno)}"`
         + ` opacity=".85"><title>${dstr(d)} ${nameOf(r.bno)} ${
           fmt(r.net, 2)} 億</title></rect>`;
    });
  });
  let c = 0; const cums = dates.map(d => c += tot[d]);
  const cMin = Math.min(0, ...cums), cMax = Math.max(0, ...cums);
  const yC = a.scale(cMin, cMax, a.P.t, a.P.t + a.ih);
  s += `<polyline fill="none" stroke="var(--accent)" stroke-width="1.6"`
     + ` points="${cums.map((v, i) => `${a.x(i)},${yC(v)}`).join(" ")}"/>`;
  s += a.lab("r", yC(cums[cums.length - 1]) + 4,
             `累積 ${fmt(cums[cums.length - 1], 0)}億`, "var(--accent)", 11);
  s += a.lab("l", a.P.t + 10, `+${mx.toFixed(0)}億`, "var(--dim)", 11);
  s += a.lab("l", a.P.t + a.ih, `−${mx.toFixed(0)}億`, "var(--dim)", 11);
  s += a.xLabels(dates);
  el.innerHTML = s + a.close()
    + '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;'
    + 'font-size:12px">'
    + keys.map(b => `<span><span style="display:inline-block;width:10px;`
        + `height:10px;background:${col(b)};border-radius:2px;`
        + `margin-right:4px"></span>${nameOf(b)}</span>`).join("")
    + "</div>";
}

/** 規模(流通單位數)與股票部位佔淨值% 疊圖 —— 分辨主動加碼與被動買進。 */
export function chartFund(el, rows) {
  if (!rows.length) return empty(el, "無資料");
  const a = axes(rows.length, {H: 240, P: {t: 14, r: 64, b: 26, l: 60},
                              spread: true});
  const wv = rows.map(r => r.stock_weight || 0);
  const w0 = Math.min(...wv), w1 = Math.max(...wv);
  const yW = a.scale(w0, w1, a.P.t, a.P.t + a.ih);
  let s = a.open();
  s += `<polyline fill="none" stroke="var(--accent)" stroke-width="1.6"`
     + ` points="${rows.map((r, i) =>
         `${a.x(i)},${yW(r.stock_weight || 0)}`).join(" ")}"/>`;
  s += a.lab("l", a.P.t + 10, `股票部位 ${w1.toFixed(1)}%`, "var(--accent)", 11)
     + a.lab("l", a.P.t + a.ih, `${w0.toFixed(1)}%`, "var(--accent)", 11);
  const has = rows.filter(r => r.units);
  if (has.length > 1) {
    const uv = has.map(r => r.units);
    const u0 = Math.min(...uv), u1 = Math.max(...uv);
    const yU = a.scale(u0, u1, a.P.t, a.P.t + a.ih);
    const pts = rows.map((r, i) => r.units ? `${a.x(i)},${yU(r.units)}` : null)
      .filter(Boolean).join(" ");
    s += `<polyline fill="none" stroke="var(--up)" stroke-width="1.6"`
       + ` stroke-dasharray="5 3" points="${pts}"/>`;
    s += a.lab("r", a.P.t + 10, `流通 ${(u1 / 1e8).toFixed(2)}億單位`,
               "var(--up)", 11)
       + a.lab("r", a.P.t + a.ih, `${(u0 / 1e8).toFixed(2)}億`, "var(--up)", 11);
  }
  s += a.xLabels(rows.map(r => r.d));
  el.innerHTML = s + a.close();
}

/** 大盤 K 線 + 三大法人現貨買賣超,共用十字線對齊上下兩張圖。
 *
 *  沒有法人資料的日子給 null 而不是 0:各表起始日不同,補 0 的話柱子消失、
 *  累積線卻會平貼在 0 一路畫過去,看起來像「這段沒有淨流入」,其實是沒資料。 */
export function chartIndex(el, px, spot) {
  if (!px.length) return empty(el, "無資料");
  const HK = 280, GAP = 18, HF = 150;
  const a = axes(px.length, {H: HK + GAP + HF, P: {l: 62, r: 62, t: 12, b: 24},
                             bwFrac: 0.62, minBw: 1.5, maxBw: 10});
  const hi = Math.max(...px.map(r => r.high)), lo = Math.min(...px.map(r => r.low));
  const yK = a.scale(lo, hi, a.P.t + 4, HK - 4);
  const sm = {}; spot.forEach(r => sm[r.d] = r);
  const nets = px.map(r => sm[r.d] ? (sm[r.d].total || 0) / 1e8 : null);
  const vals = nets.filter(v => v != null);
  const nMax = Math.max(...vals.map(Math.abs), 1);
  const fTop = HK + GAP, fMid = fTop + (HF - a.P.b) / 2;
  const yBar = a.scale(-nMax, nMax, fTop + 4, fTop + HF - a.P.b - 4);
  let c = 0;
  const cums = nets.map(v => v == null ? null : (c += v));
  const cvals = cums.filter(v => v != null);
  const cMin = Math.min(0, ...cvals), cMax = Math.max(0, ...cvals);
  const yCum = a.scale(cMin, cMax, fTop, fTop + HF - a.P.b);
  const firstIdx = nets.findIndex(v => v != null);
  let s = a.open();
  px.forEach((r, i) => {
    const col = r.close >= r.open ? "var(--up)" : "var(--dn)";
    const yo = yK(r.open), yc = yK(r.close);
    s += `<line x1="${a.x(i)}" y1="${yK(r.high)}" x2="${a.x(i)}"`
       + ` y2="${yK(r.low)}" stroke="${col}" stroke-width="1"/>`
       + `<rect x="${a.x(i) - a.bw / 2}" y="${Math.min(yo, yc)}"`
       + ` width="${a.bw}" height="${Math.max(1, Math.abs(yc - yo))}"`
       + ` fill="${col}"/>`;
  });
  const nf = v => v.toLocaleString(undefined, {maximumFractionDigits: 0});
  s += a.lab("l", a.P.t + 8, nf(hi)) + a.lab("l", HK - 2, nf(lo));
  s += a.zero(fMid);
  nets.forEach((v, i) => {
    if (v == null) return;
    const y1 = yBar(v);
    s += `<rect x="${a.x(i) - a.bw / 2}" y="${Math.min(fMid, y1)}"`
       + ` width="${a.bw}" height="${Math.abs(y1 - fMid)}"`
       + ` fill="${v >= 0 ? 'var(--up)' : 'var(--dn)'}" opacity=".8"/>`;
  });
  // 累積線只畫在有資料的區段,不從圖表左緣拉一條 0 過來
  const cpts = cums.map((v, i) => v == null ? null : `${a.x(i)},${yCum(v)}`)
    .filter(Boolean);
  if (cpts.length > 1)
    s += `<polyline fill="none" stroke="var(--accent)" stroke-width="1.5"`
       + ` points="${cpts.join(" ")}"/>`;
  s += a.lab("l", fTop + 10, `三大法人 ±${nMax.toFixed(0)}億`);
  if (firstIdx > 0)
    s += `<text x="${a.x(firstIdx) + 4}" y="${fTop + 24}" fill="var(--dim)"`
       + ` font-size="10">← 法人資料自 ${px[firstIdx].d} 起</text>`;
  if (cvals.length)
    s += a.lab("r", yCum(cvals[cvals.length - 1]) + 4,
               `累積 ${fmt(cvals[cvals.length - 1], 0)}億`, "var(--accent)");
  s += a.xLabels(px.map(r => +String(r.d).replaceAll("-", "")), a.H - 6);
  s += a.cross() + a.hits();
  el.innerHTML = s + a.close();
  a.bind(el, i => {
    const r = px[i], sp = sm[r.d];
    const yi = v => (v || 0) / 1e8;
    let t = `<b>${r.d}</b><br>開 ${nf(r.open)} 高 ${nf(r.high)} 低 ${
      nf(r.low)} <b>收 ${nf(r.close)}</b>`
      + ` <span class="${r.pts >= 0 ? 'up' : 'dn'}">${fmt(r.pts, 0)}（${
        fmt(r.pct, 2)}%）</span><br>成交 ${nf(yi(r.amount))} 億`;
    if (sp) t += `<br>外資 <b class="${yi(sp.foreign) >= 0 ? 'up' : 'dn'}">${
      fmt(yi(sp.foreign), 1)}</b> 投信 <b class="${
      yi(sp.trust) >= 0 ? 'up' : 'dn'}">${fmt(yi(sp.trust), 1)}</b>`
      + ` 自營 <b class="${yi(sp.dealer) >= 0 ? 'up' : 'dn'}">${
        fmt(yi(sp.dealer), 1)}</b><br>合計 <b class="${
        yi(sp.total) >= 0 ? 'up' : 'dn'}">${fmt(yi(sp.total), 1)} 億</b>`;
    return t;
  });
}

/* ---------- 圓餅 ---------- */
export const PIE_HUES = [210, 0, 145, 35, 275, 190, 60, 320, 105, 15, 240, 165];
export const pieColor = i => `hsl(${PIE_HUES[i % PIE_HUES.length]} 62% 52%)`;

/** 圓餅圖。items = [{label, value, color}]。
 *  刻意不畫小於 1.5% 的扇形標籤 —— 標了會互相重疊反而看不懂,細節看表格。 */
export function pie(el, title, items, unit) {
  const tot = items.reduce((s, x) => s + x.value, 0);
  if (!tot) { el.innerHTML = `<div class="spin">${title}:無資料</div>`; return; }
  const R = 78, C = 96, W = 320;
  let a0 = -Math.PI / 2, s = `<svg viewBox="0 0 ${W} 210">`;
  items.forEach(it => {
    const frac = it.value / tot, a1 = a0 + frac * Math.PI * 2;
    const x0 = C + R * Math.cos(a0), y0 = C + R * Math.sin(a0);
    const x1 = C + R * Math.cos(a1), y1 = C + R * Math.sin(a1);
    // 整圓時 A 弧線會退化成一個點,改用 circle
    s += frac >= 0.9999
      ? `<circle cx="${C}" cy="${C}" r="${R}" fill="${it.color}"/>`
      : `<path d="M${C},${C} L${x0.toFixed(1)},${y0.toFixed(1)} `
        + `A${R},${R} 0 ${frac > 0.5 ? 1 : 0} 1 ${x1.toFixed(1)},${
          y1.toFixed(1)} Z" fill="${it.color}" stroke="var(--card)"`
        + ` stroke-width="1"/>`;
    if (frac >= 0.015) {
      const am = (a0 + a1) / 2;
      s += `<text x="${(C + R * 0.62 * Math.cos(am)).toFixed(1)}"`
        + ` y="${(C + R * 0.62 * Math.sin(am)).toFixed(1)}" font-size="9"`
        + ` fill="#fff" text-anchor="middle">${(frac * 100).toFixed(0)}%</text>`;
    }
    a0 = a1;
  });
  s += `<text x="${C}" y="196" font-size="11" fill="var(--dim)"`
     + ` text-anchor="middle">${title}</text></svg>`;
  s += '<div style="font-size:11px;line-height:1.7">';
  items.forEach(it => {
    s += `<div><span style="display:inline-block;width:9px;height:9px;`
      + `background:${it.color};border-radius:2px;margin-right:5px"></span>${
      it.label} <span class="dim">${(it.value / tot * 100).toFixed(1)}%`
      + `${unit ? " · " + unit(it.value) : ""}</span></div>`;
  });
  el.innerHTML = s + "</div>";
}
