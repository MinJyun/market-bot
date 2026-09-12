/** 表格渲染。
 *
 *  原本 6 個渲染器共 148 行,做的事高度相似:表頭 + 逐列 + 數字上色 + 長條 +
 *  可點擊。收成一個 table(el, rows, cols) —— 欄位用規格描述,各表只給規格。
 *
 *  `tableInst` 是唯一的例外,它是兩層表頭(契約 × 身份別)的交叉表,硬套規格
 *  只會讓規格變成另一種模板語言,所以維持獨立實作。
 */
import {fmt, cls, dstr, lots, numf, toInt, delta} from "./lib.js";

/** cols 每欄:{h 標題, num 右對齊, get 取值, html 自訂內容, cls 上色, title,
 *  width}。opt:{empty 空訊息, scroll 橫向捲動, pick 取列 id, onRow 點擊}。 */
export function table(el, rows, cols, opt = {}) {
  if (!rows.length) {
    el.innerHTML = `<div class="spin">${opt.empty || "無資料"}</div>`;
    return;
  }
  let s = (opt.scroll ? '<div class="scroll">' : "") + "<table><tr>";
  cols.forEach(c => {
    s += `<th${c.num ? ' class="num"' : ""}${
      c.width ? ` style="width:${c.width}"` : ""}>${c.h ?? "　"}</th>`;
  });
  s += "</tr>";
  rows.forEach(r => {
    s += `<tr${opt.pick ? ` class="pick" data-id="${opt.pick(r)}"` : ""}>`;
    cols.forEach(c => {
      const kls = [c.num ? "num" : "", c.cls ? c.cls(r) : ""].filter(Boolean);
      s += `<td${kls.length ? ` class="${kls.join(" ")}"` : ""}${
        c.title ? ` title="${c.title(r)}"` : ""}>${
        c.html ? c.html(r) : (c.get(r) ?? "")}</td>`;
    });
    s += "</tr>";
  });
  el.innerHTML = s + "</table>" + (opt.scroll ? "</div>" : "");
  if (opt.pick && opt.onRow) el.querySelectorAll("tr.pick").forEach(tr =>
    tr.onclick = () => opt.onRow(tr.dataset.id));
}

/** 名稱 + 淡色代號。分點與個股共用(取 bno 或 stock_id)。 */
const nameCell = r => {
  const id = r.stock_id || r.bno || "";
  return (r.name || id) + (r.name && id ? ` <span class="dim">${id}</span>` : "");
};

/** 橫向長條格,寬度依該欄最大絕對值歸一化。 */
const barCell = (r, v, mx) =>
  `<div class="bar" style="width:${Math.abs(v) / mx * 100}%;background:${
    v >= 0 ? 'var(--up)' : 'var(--dn)'};opacity:.7"></div>`;

/** 買/賣/淨張 + 淨額 + 長條。rows 有 buy_sh 時才顯示張數三欄。 */
export function tableBars(el, rows, key, label, onRow) {
  const mx = Math.max(...rows.map(r => Math.abs(r[key])), 1e-9);
  const hasLots = rows.some(r => r.buy_sh !== undefined);
  const net = r => lots(r.buy_sh) - lots(r.sell_sh);
  const cols = [{h: label, html: nameCell}];
  if (hasLots) cols.push(
    {h: "買張", num: true, cls: () => "dim", get: r => numf(lots(r.buy_sh))},
    {h: "賣張", num: true, cls: () => "dim", get: r => numf(lots(r.sell_sh))},
    {h: "淨張", num: true, cls: r => cls(net(r)),
     get: r => (net(r) >= 0 ? "+" : "") + numf(net(r))});
  cols.push(
    {h: "淨額(億)", num: true, cls: r => cls(r[key]), get: r => fmt(r[key], 2)},
    {width: "16%", html: r => barCell(r, r[key], mx)});
  table(el, rows, cols, {pick: onRow ? (r => r.bno || r.stock_id || "") : null,
                         onRow});
}

/** 損益排行:已實現/未實現/合計。依合計排序(只看已實現會漏掉抱著獲利的部位)。
 *  未實現為 null = 淨賣超,賣出的是既有庫存、成本不可知,合計僅計已實現。 */
export function tablePnl(el, rows, label, onRow) {
  const mx = Math.max(...rows.map(r => Math.abs(r.pnl)), 1e-9);
  table(el, rows, [
    {h: label, html: nameCell},
    {h: "相抵張", num: true, cls: () => "dim",
     get: r => numf(r.matched_sh / 1000)},
    {h: "已實現", num: true, cls: r => cls(r.real), get: r => fmt(r.real, 2)},
    {h: "未實現", num: true, cls: r => r.unreal == null ? "dim" : cls(r.unreal),
     title: r => r.unreal == null
       ? "淨賣超:賣出的是既有庫存,成本不可知,不估未實現" : "",
     get: r => r.unreal == null ? "—" : fmt(r.unreal, 2)},
    {h: "合計(億)", num: true, cls: r => cls(r.pnl),
     html: r => `<b>${fmt(r.pnl, 2)}</b>` + (r.unreal == null
       ? '<span class="dim" title="僅計已實現">*</span>' : "")},
    {width: "14%", html: r => barCell(r, r.pnl, mx)},
  ], {empty: "此區間無資料",
      pick: onRow ? (r => r.stock_id || r.bno || "") : null, onRow});
}

/** 跨 ETF 共識:先看「幾檔 ETF 動作」再看金額 —— 單一經理人的大單不是共識。 */
export function tableConsensus(el, rows, cntKey) {
  const mx = Math.max(...rows.map(r => Math.abs(r.net || 0)), 1e-9);
  table(el, rows, [
    {h: "股票", html: r => nameCell(r) + (r.corp
      ? ' <span class="dim" title="區間內有分割/減資等公司行動,'
        + '股數已還原為實際買賣">⚠</span>' : "")},
    {h: "幾檔", num: true, html: r => `<b>${r[cntKey]}</b>`},
    {h: "淨張", num: true, cls: r => cls(r.net_sh),
     get: r => (r.net_sh >= 0 ? "+" : "") + numf(lots(r.net_sh))},
    // 沒有日線的成分股(海外 ETF 的台股外持股不在股票池)金額算不出來
    {h: "淨額(億)", num: true, cls: r => r.net == null ? "dim" : cls(r.net),
     get: r => r.net == null ? "—" : fmt(r.net, 2)},
    {h: "佔量%", num: true, cls: () => "dim",
     get: r => r.pct_vol == null ? "—" : r.pct_vol.toFixed(2)},
    {width: "14%", html: r => barCell(r, r.net || 0, mx)},
  ], {empty: "此區間無資料"});
}

/** 三大法人未平倉交叉表:每列一天、每個契約三欄(外資/投信/自營)。
 *  值為未平倉存量,滑鼠停在數字上顯示當日交易淨額;小字為前日增減。 */
export function tableInst(el, rows, keyName) {
  if (!rows.length) { el.innerHTML = '<div class="spin">無資料</div>'; return; }
  const days = [...new Set(rows.map(r => r.d))].sort().reverse().slice(0, 20);
  const by = {}; rows.forEach(r => by[r.d + "|" + r[keyName]] = r);
  const keys = [...new Set(rows.map(r => r[keyName]))];
  const WHO = ["foreign", "trust", "dealer"];
  let s = '<div class="scroll"><table><tr><th>日期</th>';
  keys.forEach(k => s += `<th class="num" colspan="3">${k}</th>`);
  s += "</tr><tr><th></th>";
  keys.forEach(() => s += '<th class="num dim">外資</th>'
    + '<th class="num dim">投信</th><th class="num dim">自營</th>');
  s += "</tr>";
  days.forEach(d => {
    s += `<tr><td>${dstr(toInt(d))}</td>`;
    keys.forEach(k => {
      const r = by[d + "|" + k];
      WHO.forEach(f => {
        const v = r ? r[f] : null, t = r ? r[f + "_t"] : null;
        s += `<td class="num ${v == null ? "dim" : cls(v)}" title="${
          t == null ? "" : "當日交易淨額 " + numf(t) + " 口"}">${
          v == null ? "—" : numf(v)}${delta(r ? r[f + "_d"] : null)}</td>`;
      });
    });
    s += "</tr>";
  });
  el.innerHTML = s + "</table></div>";
}

/** 十大交易人淨部位。6~10 名 = 前十大 − 前五大(後端已算好)。 */
export function tableLt(el, rows, contract) {
  const rs = rows.filter(r => r.contract === contract).slice().reverse()
    .slice(0, 20);
  const num = (h, k) => ({h, num: true, cls: r => cls(r[k]),
                          html: r => numf(r[k]) + delta(r[k + "_d"])});
  table(el, rs, [
    {h: "日期", get: r => dstr(toInt(r.d))},
    num("前5大特法", "net5"), num("前10大特法", "net10"),
    num("6~10名特法", "net6_10"),
    num("前5大全部", "net5_all"), num("前10大全部", "net10_all"),
    {h: "全market OI", num: true, cls: () => "dim",
     html: r => numf(r.oi || 0) + delta(r.oi_d)},
  ], {scroll: true});
}

export function tablePcr(el, rows) {
  table(el, rows.slice().reverse().slice(0, 20), [
    {h: "日期", get: r => dstr(toInt(r.d))},
    {h: "OI Ratio(%)", num: true,
     html: r => (r.oi == null ? "—" : r.oi.toFixed(2)) + delta(r.oi_d, 2)},
    {h: "Vol Ratio(%)", num: true, cls: () => "dim",
     html: r => (r.vol == null ? "—" : r.vol.toFixed(2)) + delta(r.vol_d, 2)},
  ], {scroll: true});
}
