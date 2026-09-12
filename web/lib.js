/** 共用工具:格式化、日期轉換、API 取值、tooltip、快捷鍵標記。
 *  這些原本散在單一 index.html 的全域 scope 裡,拆成 module 後由各處 import。
 */
const $ = s => document.querySelector(s);
const fmt = (v, d=1) => (v>=0?"+":"") + v.toFixed(d);
const cls = v => v>0 ? "up" : (v<0 ? "dn" : "dim");
const dstr = d => String(d).slice(4,6)+"/"+String(d).slice(6,8);

async function get(path, params={}) {
  const q = new URLSearchParams(params).toString();
  const r = await fetch(path + (q?"?"+q:""));
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}
const TIP = () => document.getElementById("tip");

function showTip(html, ev) {
  const t = TIP();
  t.innerHTML = html;
  t.style.display = "block";
  const pad = 14, w = t.offsetWidth, h = t.offsetHeight;
  let left = ev.clientX + pad, top = ev.clientY + pad;
  if (left + w > innerWidth) left = ev.clientX - w - pad;
  if (top + h > innerHeight) top = ev.clientY - h - pad;
  t.style.left = left + "px"; t.style.top = top + "px";
}
const hideTip = () => { TIP().style.display = "none"; };
const lots = sh => (sh || 0) / 1000;              // 1 張 = 1,000 股
const numf = (v, d=0) => v.toLocaleString(undefined,
  {minimumFractionDigits:d, maximumFractionDigits:d});
// 傳入「萬元」,自動換單位:1 億 = 10,000 萬。不換的話會出現 +286350 萬
// 這種讀不出量級的數字。
const wan = w => Math.abs(w) >= 10000 ? fmt(w/10000,2)+" 億" : fmt(w,0)+" 萬";
// 日期在 DB 是 YYYYMMDD 整數,<input type="date"> 用 YYYY-MM-DD,兩邊互轉
const toISO = d => `${String(d).slice(0,4)}-${String(d).slice(4,6)}-${String(d).slice(6,8)}`;
const toInt = s => +String(s).replaceAll("-","");

/** 從一串有資料的日期取最後 n 天的區間([from, to]);n=0 表示全部。 */
function windowOf(dates, n) {
  if (!dates.length) return null;
  return [n > 0 ? dates[Math.max(0, dates.length - n)] : dates[0],
          dates[dates.length - 1]];
}

function markQuick(sel, n) {
  // 每顆快捷鍵只帶一個 data-* 屬性(q/sq/pq/eq/bpq/spq/sfq),直接取第一個值
  // 比較 —— 原本是一長串 OR,每加一組快捷鍵就得回來改一次,漏了就不會亮。
  document.querySelectorAll(sel + " button").forEach(b =>
    b.className = (+Object.values(b.dataset)[0] === n) ? "on" : "");
}
const 億 = v => (v || 0) / 1e8;
/** 前日增減的小字標註。null(前一日缺資料)不顯示 —— 用 0 冒充「沒變動」
 *  會讓資料缺口看起來像盤整。 */
const delta = (v, d=0) => v==null ? "" :
  `<br><span class="dim" style="font-size:11px">前日 <span class="${
    cls(v)}">${fmt(v,d)}</span></span>`;

export {$, fmt, cls, dstr, get, TIP, showTip, hideTip, lots, numf, wan,
        toISO, toInt, windowOf, markQuick, 億, delta};
