/** 各視角的載入與渲染(依分點 / 依個股 / 主動式ETF / 大盤)。
 *
 *  這一段是從原本單一 index.html 的 <script> 原封不動搬過來的,只改三處:
 *  加上 import、chartStack 的分點名稱改由呼叫端傳入(原本直接讀全域 META)、
 *  以及把 pie/pieColor 從本檔的定義換成 import。
 */
import {$, fmt, cls, dstr, get, numf, wan, toISO, toInt, windowOf,
        markQuick, 億} from "./lib.js";
import {chartFlow, chartCandle, chartStack, chartFund, chartIndex,
        pie, pieColor} from "./charts.js";
import {tableBars, tablePnl, tableConsensus, tableInst, tableLt,
        tablePcr} from "./tables.js";
import {initDemo} from "./demo.js";

let META = null;

/* ---------- 依分點 ---------- */
let B_CACHE = null;      // 快取整段 daily,改日期只在前端切片,不重打 API

let RANK_SEQ = 0;   // 連點日期時,只讓最後一次請求的結果生效
let BROKERS = [];   // [{bno,name,net,...}],供搜尋比對

/** 把使用者輸入(代號、名稱、或 datalist 選到的「名稱（代號）」)解析成 bno。 */
function pickBno(raw) {
  const q = (raw || "").trim();
  if (!q) return BROKERS[0] && BROKERS[0].bno;
  const m = q.match(/[（(]([0-9A-Za-z]{4})[）)]\s*$/);   // 「元大（9800）」
  if (m) return m[1];
  const exact = BROKERS.find(b => b.bno.toLowerCase() === q.toLowerCase());
  if (exact) return exact.bno;
  const byName = BROKERS.find(b => b.name === q);
  if (byName) return byName.bno;
  const part = BROKERS.find(b => (b.name || "").includes(q) ||
                                 b.bno.toLowerCase().includes(q.toLowerCase()));
  return part ? part.bno : null;
}

function renderBroker() {
  if (!B_CACHE) return;
  const f = $("#d-from").value ? toInt($("#d-from").value) : 0;
  const t = $("#d-to").value ? toInt($("#d-to").value) : 99999999;
  const daily = B_CACHE.daily.filter(x => x.d >= f && x.d <= t);
  const j = B_CACHE;
  $("#b-title").textContent =
    `${j.name||j.bno}（${j.bno}）　${daily.length} 個交易日` +
    (daily.length ? `　${toISO(daily[0].d)} ~ ${toISO(daily[daily.length-1].d)}` : "");
  chartFlow($("#b-chart"), daily, jumpToDay);
  loadRank(f, t);      // 個股排行由後端按區間重算,不能沿用全期間的結果
}

async function loadRank(f, t) {
  const seq = ++RANK_SEQ;
  const eb = $("#b-buy"), es = $("#b-sell");
  eb.classList.add("stale"); es.classList.add("stale");
  const j = await get("/api/broker",
    {bno: CUR_BNO, from: f, to: t, topn: $("#sel-topn").value});
  if (seq !== RANK_SEQ) return;              // 已有更新的請求,丟棄本次
  eb.classList.remove("stale"); es.classList.remove("stale");
  $("#b-rank-hd").textContent =
    `該分點進出最大的個股（${toISO(f===0?B_CACHE.daily[0].d:f)} ~ ${
      toISO(t===99999999?B_CACHE.daily[B_CACHE.daily.length-1].d:t)}）`;
  const sumOf = a => a.reduce((x,y)=>x+y.net, 0);
  $("#b-buy-hd").textContent =
    `買超 ${j.top_buy.length}/${j.n_buy} 檔　合計 ${fmt(sumOf(j.top_buy),0)} 億`;
  $("#b-sell-hd").textContent =
    `賣超 ${j.top_sell.length}/${j.n_sell} 檔　合計 ${fmt(sumOf(j.top_sell),0)} 億`;
  const openPair = sid => showPair(CUR_BNO, sid);
  tableBars(eb, j.top_buy, "net", "個股", openPair);
  tableBars(es, j.top_sell, "net", "個股", openPair);
}

let BP_DAYS = 60, BP_SEQ = 0;
async function loadBrokerPnl() {
  const seq = ++BP_SEQ;
  if (!CUR_BNO || !B_CACHE || !B_CACHE.daily.length) return;
  const w = windowOf(B_CACHE.daily.map(x=>x.d), BP_DAYS);
  $("#b-gain").innerHTML = $("#b-loss").innerHTML =
    '<div class="spin">載入中…</div>';
  const j = await get("/api/broker/pnl",
    {bno: CUR_BNO, from: w[0], to: w[1], topn: $("#sel-topn").value});
  if (seq !== BP_SEQ) return;
  $("#b-pnl-hd").textContent =
    `該分點在個股上的損益排行（${toISO(w[0])} ~ ${toISO(w[1])}）`;
  $("#b-pnl-sum").textContent = j.pnl_total==null ? "" :
    `全部 ${j.n_gain+j.n_loss} 檔合計 ${fmt(j.pnl_total,1)} 億`;
  $("#b-gain-hd").textContent = `獲利 ${j.top_gain.length}/${j.n_gain} 檔`;
  $("#b-loss-hd").textContent = `虧損 ${j.top_loss.length}/${j.n_loss} 檔`;
  $("#b-pnl-tip").innerHTML = pnlTip(j.px_date);
  const openPair = sid => showPair(CUR_BNO, sid);
  tablePnl($("#b-gain"), j.top_gain, "個股", openPair);
  tablePnl($("#b-loss"), j.top_loss, "個股", openPair);
}

/** 損益表的共用免責說明 —— 這組數字最容易被當成「某主力賺賠多少」。 */
function pnlTip(pxDate) {
  return `已實現 = 買賣相抵股數 ×(賣均價−買均價);未實現 = 剩餘淨部位以`
    + ` ${pxDate?toISO(pxDate):"期末"} 收盤價評價。<b>只計算這段區間內的進出</b>,`
    + `刻意不推算持有成本 —— 資料起於 2025-07-28,多數分點在那之前就有存貨。`
    + `未實現<b>只在淨買超時才算</b>;標 <b>*</b> 的是淨賣超,`
    + `賣出的是既有庫存、成本不可知,合計僅計已實現。`
    + `一個分點是所有客戶的合計,混著不同成本的投資人、當沖與借券賣出,`
    + `所以這是<b>下單通道整體的紙上損益</b>,不是某個主力的損益。`;
}

let CUR_BNO = null;

async function loadBroker(keepDates) {
  const bno = CUR_BNO;
  if (!bno) return;
  $("#b-chart").className="spin"; $("#b-chart").textContent="載入中…";
  B_CACHE = await get("/api/broker",{bno});
  $("#b-chart").className="";
  const ds = B_CACHE.daily.map(x=>x.d);
  if (ds.length) {
    // 日期輸入的可選範圍鎖在實際資料範圍內,避免選到空區間
    $("#d-from").min = $("#d-to").min = toISO(ds[0]);
    $("#d-from").max = $("#d-to").max = toISO(ds[ds.length-1]);
    if (!keepDates || !$("#d-from").value) setQuick(60);
  }
  renderBroker();
  loadBrokerPnl();          // 獨立區間,不跟著主圖的日期走
}

function setQuick(n) {
  if (!B_CACHE || !B_CACHE.daily.length) return;
  markQuick("#q-days", n);
  const ds = B_CACHE.daily.map(x=>x.d);
  const from = n>0 ? ds[Math.max(0, ds.length-n)] : ds[0];
  $("#d-from").value = toISO(from);
  $("#d-to").value = toISO(ds[ds.length-1]);
}

/** 點柱狀圖某一天 → 顯示該分點「當日」的個股明細(主圖區間不變)。 */
async function jumpToDay(d) {
  const card = $("#b-day");
  card.style.display = "";
  $("#b-day-hd").textContent = `${B_CACHE.name||B_CACHE.bno} 當日明細 ${toISO(d)}`;
  $("#b-day-buy").innerHTML = '<div class="spin">載入中…</div>';
  $("#b-day-sell").innerHTML = "";
  const j = await get("/api/broker",
    {bno: CUR_BNO, from: d, to: d, topn: $("#sel-topn").value});
  const sumOf = a => a.reduce((x,y)=>x+y.net, 0);
  $("#b-day-buy-hd").textContent =
    `買超 ${j.top_buy.length}/${j.n_buy} 檔　合計 ${fmt(sumOf(j.top_buy),1)} 億`;
  $("#b-day-sell-hd").textContent =
    `賣超 ${j.top_sell.length}/${j.n_sell} 檔　合計 ${fmt(sumOf(j.top_sell),1)} 億`;
  // 與主排行一致:點個股下鑽到「該分點 × 該個股」的 K 線與進出
  const openPair = sid => showPair(CUR_BNO, sid);
  tableBars($("#b-day-buy"), j.top_buy, "net", "個股", openPair);
  tableBars($("#b-day-sell"), j.top_sell, "net", "個股", openPair);
  card.scrollIntoView({behavior:"smooth", block:"start"});
}

/* ---------- 依個股 ---------- */

let S_DATES = [];    // 該股所有有資料的日期(給快捷鈕用)
let STOCKS = [];     // [{stock_id,name}]
let CUR_SID = null;  // 目前選取的個股(輸入框選完即清空,狀態靠這個變數與標題)

/** 把輸入(代號、名稱、或「2330 台積電」)解析成 stock_id。 */
function pickSid(raw) {
  const q = (raw || "").trim();
  if (!q) return null;
  const m = q.match(/^([0-9A-Za-z]{4})\b/);          // 開頭就是代號
  if (m && STOCKS.some(x => x.stock_id === m[1])) return m[1];
  const exact = STOCKS.find(x => x.stock_id === q || x.name === q);
  if (exact) return exact.stock_id;
  const part = STOCKS.find(x => (x.name||"").includes(q) ||
                                x.stock_id.includes(q));
  return part ? part.stock_id : null;
}

function setStockQuick(n) {
  if (!S_DATES.length) return;
  markQuick("#s-days", n);
  const from = n > 0 ? S_DATES[Math.max(0, S_DATES.length - n)] : S_DATES[0];
  $("#s-from").value = toISO(from);
  $("#s-to").value = toISO(S_DATES[S_DATES.length - 1]);
}

let STOCK_SEQ = 0;
async function loadStock(initDates) {
  const seq = ++STOCK_SEQ;
  const stock_id = CUR_SID;
  if (!stock_id) return;
  const p = {stock_id, topn: $("#s-topn").value};
  if (!initDates && $("#s-from").value) {
    p.from = toInt($("#s-from").value);
    p.to = toInt($("#s-to").value || $("#s-from").value);
  }
  $("#s-buy").classList.add("stale"); $("#s-sell").classList.add("stale");
  const j = await get("/api/stock", p);
  if (seq !== STOCK_SEQ) return;
  $("#s-buy").classList.remove("stale"); $("#s-sell").classList.remove("stale");
  S_DATES = j.dates;
  if (S_DATES.length) {
    $("#s-from").min = $("#s-to").min = toISO(S_DATES[0]);
    $("#s-from").max = $("#s-to").max = toISO(S_DATES[S_DATES.length-1]);
    if (initDates) {                       // 換股票時預設回最新一日
      $("#s-from").value = toISO(j.range[0]);
      $("#s-to").value = toISO(j.range[1]);
    }
  }
  if (initDates) { loadStockPnl(); loadStockFeatured(); }   // 各自獨立區間
  const [a, b] = j.range;
  $("#s-title").textContent = `${j.name||stock_id}（${stock_id}）　` +
    (a === b ? toISO(a) : `${toISO(a)} ~ ${toISO(b)}`);
  const sumOf = arr => arr.reduce((x,y)=>x+y.net, 0);
  $("#s-buy-hd").textContent =
    `買超分點 ${j.buyers.length}/${j.n_buy}　合計 ${fmt(sumOf(j.buyers),1)} 億`;
  $("#s-sell-hd").textContent =
    `賣超分點 ${j.sellers.length}/${j.n_sell}　合計 ${fmt(sumOf(j.sellers),1)} 億`;
  const pc = j.conc_pct, cl = j.conc_lots;
  const px = j.price && j.price.length ? j.price[j.price.length-1].close : null;
  $("#s-conc").innerHTML =
    `<div><div class="k">籌碼集中(前${j.conc_n}大)</div>
       <div class="v ${cl==null?"dim":cls(cl)}">${
         cl==null?"無分點資料":fmt(cl,0)+" 張"}</div></div>
     <div><div class="k">籌碼集中度</div>
       <div class="v ${pc==null?"dim":cls(pc)}">${
         pc==null?"—":fmt(pc,2)+"%"}</div></div>
     <div><div class="k">區間成交量<span class="dim">(${j.vol_src})</span></div>
       <div class="v">${j.vol_lots==null?"—":numf(j.vol_lots)+" 張"}</div></div>
     <div><div class="k">期末收盤</div>
       <div class="v">${px==null?"—":px.toLocaleString()}</div></div>
     <div><div class="k">買超分點</div><div class="v up">${j.n_buy}</div></div>
     <div><div class="k">賣超分點</div><div class="v dn">${j.n_sell}</div></div>`;
  const openB = b => showPair(b, CUR_SID);
  tableBars($("#s-buy"), j.buyers, "net", "分點", openB);
  tableBars($("#s-sell"), j.sellers, "net", "分點", openB);
}

// 個股頁的損益排行與重點分點圖各有獨立區間,都預設 60 天。與上方頁籤刻意
// 不同步:上面預設只看最新一日(看當天籌碼),但損益與趨勢看一天沒意義。
let SP_DAYS = 60, SP_SEQ = 0, SF_DAYS = 60, SF_SEQ = 0;

async function loadStockPnl() {
  const seq = ++SP_SEQ;
  if (!CUR_SID || !S_DATES.length) return;
  const w = windowOf(S_DATES, SP_DAYS);
  $("#s-gain").innerHTML = $("#s-loss").innerHTML =
    '<div class="spin">載入中…</div>';
  const j = await get("/api/stock/pnl",
    {stock_id: CUR_SID, from: w[0], to: w[1], topn: $("#s-topn").value});
  if (seq !== SP_SEQ) return;
  $("#s-pnl-hd").textContent =
    `各分點在此股的損益排行（${toISO(w[0])} ~ ${toISO(w[1])}）`;
  $("#s-pnl-sum").textContent = j.pnl_total==null ? "" :
    `全部 ${j.n_gain+j.n_loss} 個分點合計 ${fmt(j.pnl_total,1)} 億`;
  $("#s-gain-hd").textContent = `獲利 ${j.top_gain.length}/${j.n_gain} 個分點`;
  $("#s-loss-hd").textContent = `虧損 ${j.top_loss.length}/${j.n_loss} 個分點`;
  $("#s-pnl-tip").innerHTML = pnlTip(j.px_date);
  const openB = b => showPair(b, CUR_SID);
  tablePnl($("#s-gain"), j.top_gain, "分點", openB);
  tablePnl($("#s-loss"), j.top_loss, "分點", openB);
}

async function loadStockFeatured() {
  const seq = ++SF_SEQ;
  if (!CUR_SID || !S_DATES.length) return;
  const w = windowOf(S_DATES, SF_DAYS);
  $("#s-chart").className = "spin"; $("#s-chart").textContent = "載入中…";
  const j = await get("/api/stock/featured",
    {stock_id: CUR_SID, from: w[0], to: w[1]});
  if (seq !== SF_SEQ) return;
  $("#s-chart").className = "";
  $("#s-feat-hd").textContent =
    `重點分點在此股的每日淨額（${toISO(w[0])} ~ ${toISO(w[1])}）`;
  chartStack($("#s-chart"), j.daily,
             b => (META.featured.find(f => f.bno === b) || {}).name || b);
}

/** 個股頁點某分點 → 顯示該分點在此股的每日進出(下方另開一張卡)。 */
// 這個面板的天數刻意與上方頁籤的日期區間**獨立**:上面是在挑「看哪一段
// 行情」,這裡是在看「這組分點×個股的完整進出軌跡」,兩者常常要不同長度。
let PAIR = null, PAIR_DAYS = 60;

async function showPair(bno, sid) {
  const card = $("#s-pair");
  card.style.display = "";
  $("#s-pair-hd").textContent = "載入中…";
  $("#s-pair-chart").innerHTML = '<div class="spin">載入中…</div>';
  $("#s-pair-kpi").innerHTML = "";
  PAIR = await get("/api/pair", {bno, stock_id: sid});
  renderPair();
  card.scrollIntoView({behavior:"smooth", block:"nearest"});
}

function renderPair() {
  if (!PAIR) return;
  const j = PAIR, bno = j.bno;
  // 取最後 N 筆有進出紀錄的交易日(0 = 全部)。api_pair 只回該組合真的有
  // 進出的日子,所以這裡的「日」是紀錄數,與主圖的日曆區間不是同一回事。
  const use = PAIR_DAYS > 0 ? j.daily.slice(-PAIR_DAYS) : j.daily;
  if (!use.length) {
    $("#s-pair-hd").textContent = `${j.name||bno} 在 ${j.stock_id}:無進出紀錄`;
    $("#s-pair-chart").innerHTML = '<div class="spin">無資料</div>';
    $("#s-pair-kpi").innerHTML = "";
    return;
  }
  const bl = use.reduce((s,x)=>s+x.buy_sh,0)/1000;
  const sl = use.reduce((s,x)=>s+x.sell_sh,0)/1000;
  const ba = use.reduce((s,x)=>s+x.buy_sh*(x.buy_vwap||0),0);
  const sa = use.reduce((s,x)=>s+x.sell_sh*(x.sell_vwap||0),0);
  const bv = bl ? ba/(bl*1000) : null, sv = sl ? sa/(sl*1000) : null;
  const last = [...use].reverse().find(x=>x.close!=null);
  $("#s-pair-hd").innerHTML =
    `${j.name||bno}　在 ${j.stock_name||j.stock_id}（${j.stock_id}）的進出` +
    `<span class="dim" style="font-weight:400;font-size:12px">　${use.length}`
    + ` 個有進出的交易日　${dstr(use[0].d)} ~ ${dstr(use[use.length-1].d)}</span>`;
  const netLots = bl - sl;
  // 損益拆成「買賣相抵」與「剩餘淨部位」兩段。這個拆法**不需要假設區間
  // 起點的部位為 0** —— 而那個假設在分點資料上幾乎必然錯:抽樣 400 組高量
  // (分點×個股),96% 的累積淨部位曾經為負、78% 負到超過峰值 10%,可見多數
  // 分點在資料起點(2025-07-28)前就有存貨。逐日推平均成本會把不存在的
  // 空頭部位當真。
  //
  //   已實現 = 相抵股數 × (賣均價 − 買均價)
  //   未實現 = 淨買超時 淨股數 × (收盤 − 買均價)
  //            淨賣超時 淨股數 × (賣均價 − 收盤)
  //
  // 恆等式(已實跑四段區間驗算,差 0 元):
  //   已實現 + 未實現 == (賣出金額 − 買進金額) + 淨股數 × 期末收盤
  const matched = Math.min(bl, sl) * 1000;
  const px = last ? last.close : null;
  const real = (bv && sv) ? matched * (sv - bv) / 1e4 : null;
  // 淨賣超不估未實現:賣掉的是既有庫存,成本不可知(見 server.trade_pnl)
  const unreal = (px == null || netLots < 0) ? null
    : (netLots > 0 ? (bv ? netLots * 1000 * (px - bv) / 1e4 : null) : 0);
  const total = (real == null) ? null : real + (unreal || 0);
  $("#s-pair-kpi").innerHTML =
    `<div><div class="k">買進</div><div class="v up">${numf(bl)} 張</div></div>
     <div><div class="k">買均價</div><div class="v">${bv?bv.toFixed(2):"—"}</div></div>
     <div><div class="k">賣出</div><div class="v dn">${numf(sl)} 張</div></div>
     <div><div class="k">賣均價</div><div class="v">${sv?sv.toFixed(2):"—"}</div></div>
     <div><div class="k">淨張</div><div class="v ${cls(netLots)}">${
       fmt(netLots,0)} 張</div></div>
     <div><div class="k">淨額</div><div class="v ${cls(ba-sa)}">${
       fmt((ba-sa)/1e8,2)} 億</div></div>
     <div><div class="k">期末收盤</div><div class="v">${
       last?last.close.toLocaleString():"—"}</div></div>
     <div><div class="k">已實現<span class="dim">(相抵 ${
       numf(matched/1000)} 張)</span></div>
       <div class="v ${real==null?"dim":cls(real)}">${
         real==null?"—":wan(real)}</div></div>
     <div><div class="k">未實現<span class="dim">(${
       netLots>0?"淨買部位":"淨賣,成本不可知"})</span></div>
       <div class="v ${unreal==null?"dim":cls(unreal)}">${
         unreal==null?"—":wan(unreal)}</div></div>
     <div><div class="k">損益合計${netLots<0?
       '<span class="dim">(僅已實現)</span>':""}</div>
       <div class="v ${total==null?"dim":cls(total)}">${
         total==null?"—":wan(total)}</div></div>`;
  chartCandle($("#s-pair-chart"), use, {buy: bv, sell: sv});
}

/* ---------- 初始化 ---------- */
/* ---------- 主動式 ETF ---------- */
let E_META = null, E_DATES = [];

function setEtfQuick(n) {
  if (!E_DATES.length) return;
  markQuick("#e-days", n);
  $("#e-from").value = toISO(E_DATES[Math.max(0, E_DATES.length - n)]);
  $("#e-to").value = toISO(E_DATES[E_DATES.length - 1]);
}

let E_SEQ = 0;
async function loadConsensus() {
  const seq = ++E_SEQ;
  const p = {topn: $("#e-topn").value};
  if ($("#e-from").value) p.from = toInt($("#e-from").value);
  if ($("#e-to").value)   p.to   = toInt($("#e-to").value);
  $("#e-buy").innerHTML = $("#e-sell").innerHTML = '<div class="spin">載入中…</div>';
  const d = await get("/api/etf/consensus", p);
  if (seq !== E_SEQ) return;                 // 連點日期時只讓最後一次生效
  $("#e-title").textContent =
    `跨 ETF 共識流向　${dstr(d.range[0])} ~ ${dstr(d.range[1])}`;
  $("#e-kpi").innerHTML =
    `<div><div class="k">被加碼個股</div><div class="v up">${d.n_buy}</div></div>
     <div><div class="k">被減碼個股</div><div class="v dn">${d.n_sell}</div></div>
     <div><div class="k">偵測到公司行動</div><div class="v">${d.n_corp}</div>
     </div>`;
  $("#e-buy-hd").textContent = `共識加碼(${d.n_buy} 檔)`;
  $("#e-sell-hd").textContent = `共識減碼(${d.n_sell} 檔)`;
  tableConsensus($("#e-buy"), d.top_buy, "n_buy");
  tableConsensus($("#e-sell"), d.top_sell, "n_sell");
}

async function loadFund() {
  const etf = $("#e-etf").value;
  const p = {etf};
  if ($("#e-date").value) p.date = toInt($("#e-date").value);
  const d = await get("/api/etf/fund", p);
  if (d.dates && d.dates.length) {
    $("#e-date").min = toISO(d.dates[0]);
    $("#e-date").max = toISO(d.dates[d.dates.length - 1]);
  }
  // 要求的日期沒有資料時後端會退到較早的一天,把輸入框同步過去免得誤會
  if (d.last) $("#e-date").value = toISO(d.last);
  $("#e-fund-title").textContent = `${d.etf} ${d.name}`;
  const last = d.daily[d.daily.length-1] || {};
  const first = d.daily[0] || {};
  const du = (last.units && first.units)
    ? (last.units-first.units)/first.units*100 : null;
  $("#e-fund-kpi").innerHTML =
    `<div><div class="k">資料日</div><div class="v">${d.last?dstr(d.last):"—"}</div></div>
     <div><div class="k">持股檔數</div><div class="v">${last.holdings||"—"}</div></div>
     <div><div class="k">股票部位</div><div class="v">${
       last.stock_weight?last.stock_weight.toFixed(1)+"%":"—"}</div></div>
     <div><div class="k">流通單位數</div><div class="v">${
       last.units?(last.units/1e8).toFixed(2)+"億":'<span class="dim">無</span>'}</div></div>
     <div><div class="k">期間申贖</div><div class="v ${du===null?"":cls(du)}">${
       du===null?'<span class="dim">—</span>':fmt(du,1)+"%"}</div></div>
     <div><div class="k">可查天數</div><div class="v">${d.daily.length}</div></div>`;
  $("#e-etf-note").textContent = d.has_units
    ? "此檔另有自家投信資料,申贖曲線可用"
    : "此檔僅有 FinMind 持股,無申贖資料";
  chartFund($("#e-fund-chart"), d.daily);
  renderPies(d);
  const hd = d.holdings.slice(0, 25);
  $("#e-hold").innerHTML = hd.length ? `<table><tr><th>成分</th>
    <th class="num">股數</th><th class="num">權重%</th></tr>` +
    hd.map(h=>`<tr><td>${h.name||h.stock_id} <span class="dim">${
      h.asset_type==="stock"?h.stock_id:h.asset_type}</span></td>
      <td class="num">${numf(h.shares)}</td>
      <td class="num">${(h.weight||0).toFixed(2)}</td></tr>`).join("") +
    "</table>" : '<div class="spin">無資料</div>';
  $("#e-moves-hd").textContent =
    `${d.last?dstr(d.last):""} 異動(${d.moves.length} 筆)`;
  $("#e-moves").innerHTML = d.moves.length ? `<table><tr><th>成分</th>
    <th class="num">買</th><th class="num">賣</th><th class="num">淨股數</th></tr>` +
    d.moves.map(m=>`<tr><td>${m.name||m.stock_id} <span class="dim">${
      m.stock_id}</span></td><td class="num dim">${numf(m.buy)}</td>
      <td class="num dim">${numf(m.sell)}</td>
      <td class="num ${cls(m.net_sh)}">${m.net_sh>=0?"+":""}${
        numf(m.net_sh)}</td></tr>`).join("") + "</table>"
    : '<div class="spin">當日無異動</div>';
}

async function initEtf() {
  if (E_META) return;
  E_META = await get("/api/etf/meta");
  // 有自家投信資料的排前面(申贖曲線畫得出來),其餘依代號
  const sorted = [...E_META.etfs].sort((a,b)=>
    (b.own-a.own) || a.etf.localeCompare(b.etf));
  $("#e-etf").innerHTML = sorted.map(e=>
    `<option value="${e.etf}">${e.own?"★ ":""}${e.etf} ${e.name}（${
      e.days} 天）</option>`).join("");
  // 日期軸取自實際有持股資料的交易日,不自己推算(會把休市日算進去)
  E_DATES = [];
  const fd = await get("/api/etf/fund", {etf: sorted[0].etf});
  E_DATES = fd.daily.map(x=>x.d);
  setEtfQuick(1);
  await Promise.all([loadConsensus(), loadFund()]);
}

/** 持股明細的三張圓餅:資產類別、前 10 大持股、單一持股集中度。 */
function renderPies(d) {
  const box = $("#e-pies");
  const hs = d.holdings || [];
  if (!hs.length) { box.innerHTML = '<div class="spin">無持股資料</div>'; return; }
  box.innerHTML = '<div id="pie-a"></div><div id="pie-b"></div><div id="pie-c"></div>';
  const yi = v => (v / 1e8).toFixed(1) + "億";
  // 用權重當基準而非市值:元大與群益的 market_value 是 0(投信不揭露)
  const TYPE = {stock: "股票", futures: "期貨", bond: "債券", repo: "附買回",
                cash: "現金", etf: "ETF", option: "選擇權", other: "其他"};
  const byType = {};
  hs.forEach(h => { const k = TYPE[h.asset_type] || h.asset_type || "未分類";
                    byType[k] = (byType[k] || 0) + (h.weight || 0); });
  pie($("#pie-a"), "資產類別(佔淨值%)",
      Object.entries(byType).sort((a, b) => b[1] - a[1])
        .map(([k, v], i) => ({label: k, value: v, color: pieColor(i)})));
  const st = hs.filter(h => h.asset_type === "stock");
  const top = st.slice(0, 10);
  const rest = st.slice(10).reduce((s, h) => s + (h.weight || 0), 0);
  pie($("#pie-b"), `前 10 大持股(佔股票部位%)`,
      top.map((h, i) => ({label: `${h.name || h.stock_id}`,
                          value: h.weight || 0, color: pieColor(i)}))
        .concat(rest > 0 ? [{label: `其餘 ${st.length - 10} 檔`, value: rest,
                             color: "hsl(0 0% 55%)"}] : []));
  // 集中度:前 5 / 6~10 / 其餘,看是押重注還是分散
  const sum = (a, b) => st.slice(a, b).reduce((s, h) => s + (h.weight || 0), 0);
  pie($("#pie-c"), "持股集中度",
      [{label: "前 5 大", value: sum(0, 5), color: pieColor(1)},
       {label: "第 6~10 大", value: sum(5, 10), color: pieColor(0)},
       {label: `第 11 名以後(${Math.max(0, st.length - 10)} 檔)`,
        value: sum(10, st.length), color: "hsl(0 0% 55%)"}]
      .filter(x => x.value > 0));
}

/* ---------- 大盤 ---------- */
let M_DATA = null, M_DAYS = 60;

function renderIndex() {
  const j = M_DATA;
  if (!j || j.error) {
    $("#m-chart").innerHTML = `<div class="spin">${j?j.error:"載入失敗"}</div>`;
    return;
  }
  const last=j.price[j.price.length-1], prev=j.price[j.price.length-2];
  $("#m-title").textContent = `加權指數（${j.range[0]} ~ ${j.range[1]}）`;
  const sp=j.inst_spot[j.inst_spot.length-1];
  $("#m-kpi").innerHTML =
    `<div><div class="k">收盤</div><div class="v">${numf(last.close,2)}</div></div>
     <div><div class="k">漲跌</div><div class="v ${cls(last.pts)}">${
       fmt(last.pts,0)}（${fmt(last.pct,2)}%）</div></div>
     <div><div class="k">成交金額</div><div class="v">${numf(億(last.amount))} 億</div></div>
     <div><div class="k">較前日量</div><div class="v ${
       prev?cls(last.amount-prev.amount):""}">${
       prev?fmt(億(last.amount-prev.amount),0)+" 億":"—"}</div></div>
     <div><div class="k">三大法人</div><div class="v ${sp?cls(sp.total):""}">${
       sp?fmt(億(sp.total),1)+" 億":"—"}</div>${
       sp&&sp.total_d!=null?`<div class="k">前日 <span class="${
         cls(sp.total_d)}">${fmt(億(sp.total_d),1)}</span> 億</div>`:""}</div>
     <div><div class="k">資料日</div><div class="v">${sp?sp.d:last.d}</div></div>`;
  $("#m-chart").className="";
  chartIndex($("#m-chart"), j.price, j.inst_spot);
  tableInst($("#m-fut"), j.fut_inst.filter(r=>r.contract===$("#m-fut-c").value),
            "contract");
  tableInst($("#m-opt"), j.opt_inst, "cp");
  tableLt($("#m-lt"), j.fut_lt.concat(j.opt_lt), $("#m-lt-c").value);
  tablePcr($("#m-pcr"), j.pc_ratio);
}

async function loadIndex() {
  $("#m-chart").className="spin"; $("#m-chart").textContent="載入中…";
  M_DATA = await get("/api/index", {days: M_DAYS});
  const j = M_DATA;
  if (!j.error) {
    // 下拉選單只在第一次(或契約集合改變時)重建,免得使用者的選擇被重設
    const fc=[...new Set(j.fut_inst.map(r=>r.contract))];
    if ($("#m-fut-c").options.length !== fc.length)
      $("#m-fut-c").innerHTML = fc.map(c=>`<option>${c}</option>`).join("");
    const lc=[...new Set(j.fut_lt.concat(j.opt_lt).map(r=>r.contract))];
    if ($("#m-lt-c").options.length !== lc.length)
      $("#m-lt-c").innerHTML = lc.map(c=>`<option>${c}</option>`).join("");
  }
  renderIndex();
}

async function init() {
  META = await get("/api/meta");
  // 全部分點都要進搜尋清單(彙總表只有 900 多列,一次取完很便宜);
  // 原本寫 80 造成「能選的分點很少」
  const brokers = await get("/api/brokers",{limit:2000});
  const feat = new Set(META.featured.map(f=>f.bno));
  // ★ 重點分點在前,其餘依代號排(而非依金額 —— 找特定分點時代號序好掃)
  const sorted = [...brokers].sort((a,b)=>
    (feat.has(b.bno)-feat.has(a.bno)) || a.bno.localeCompare(b.bno));
  BROKERS = sorted;
  $("#meta").textContent =
    `${META.date_min} ~ ${META.date_max}　${META.days} 個交易日　`+
    `${META.stocks} 檔　${BROKERS.length} 個分點可查　${META.rows.toLocaleString()} 列`;
  // datalist 的 value 用「名稱（代號）」,使用者打名稱或代號都能篩到
  $("#bno-list").innerHTML = sorted.map(b=>{
    const nm=b.name||b.bno;
    const star=feat.has(b.bno)?"★ ":"";
    return `<option value="${star}${nm}（${b.bno}）">淨 ${
      b.net>=0?"+":""}${b.net.toFixed(0)} 億</option>`;}).join("");
  // 輸入框刻意留空:它是「搜尋框」不是「目前值」。填了值的話要再搜必須
  // 先把字刪掉,很難用。目前選到哪一個由下方標題顯示。
  CUR_BNO = sorted[0].bno;
  $("#q-bno").value = "";
  const stocks = await get("/api/stocks");
  STOCKS = stocks;
  $("#stock-list").innerHTML = stocks.map(x=>
    `<option value="${x.stock_id} ${x.name}"></option>`).join("");
  CUR_SID = stocks[0] && stocks[0].stock_id;
  $("#q-stock").value = "";
  await loadBroker();
  // 換分點:保留使用者選的日期區間(不要跳回預設)
  const onPickBno = () => {
    const raw = $("#q-bno").value.trim();
    if (!raw) return;
    const b = pickBno(raw);
    if (!b) { $("#b-title").textContent = `找不到符合「${raw}」的分點`; return; }
    $("#q-bno").value = "";        // 選完清空,才能直接再搜下一個
    if (b === CUR_BNO) return;
    CUR_BNO = b;
    loadBroker(true);
  };
  $("#q-bno").onchange = onPickBno;                    // 從 datalist 選取
  $("#q-bno").addEventListener("keydown", e => {       // 直接打字後按 Enter
    if (e.key === "Enter") { e.preventDefault(); onPickBno(); }
  });
  $("#sel-topn").onchange = () => { renderBroker(); loadBrokerPnl(); };
  $("#b-day-close").onclick = () => { $("#b-day").style.display = "none"; };
  $("#s-pair-close").onclick = () => { $("#s-pair").style.display = "none"; };
  document.querySelectorAll("#p-days button[data-pq]").forEach(b=>{
    b.onclick = () => { PAIR_DAYS = +b.dataset.pq;
      markQuick("#p-days", PAIR_DAYS); renderPair(); };
  });
  document.querySelectorAll("#bp-days button[data-bpq]").forEach(b=>{
    b.onclick = () => { BP_DAYS = +b.dataset.bpq;
      markQuick("#bp-days", BP_DAYS); loadBrokerPnl(); };
  });
  document.querySelectorAll("#sp-days button[data-spq]").forEach(b=>{
    b.onclick = () => { SP_DAYS = +b.dataset.spq;
      markQuick("#sp-days", SP_DAYS); loadStockPnl(); };
  });
  document.querySelectorAll("#sf-days button[data-sfq]").forEach(b=>{
    b.onclick = () => { SF_DAYS = +b.dataset.sfq;
      markQuick("#sf-days", SF_DAYS); loadStockFeatured(); };
  });
  $("#d-from").onchange = renderBroker;
  $("#d-to").onchange = renderBroker;
  // 必須鎖定 #q-days 且限定 data-q:原本寫 ".quick button"(無屬性過濾),
  // 會把頁面上每一組快捷鍵都綁成「改分點頁區間」,而且因為這行在後面執行,
  // 直接覆蓋掉 pair 面板與 ETF 頁自己的 handler。點 pair 的天數會變成
  // setQuick(NaN) → 日期框被寫成空值 → 重繪上面的圖(下面反而沒動)。
  document.querySelectorAll("#q-days button[data-q]").forEach(b => {
    b.onclick = () => { setQuick(+b.dataset.q); renderBroker(); };
  });
  const onPickSid = () => {
    const raw = $("#q-stock").value.trim();
    if (!raw) return;
    const sid = pickSid(raw);
    if (!sid) { $("#s-title").textContent = `找不到符合「${raw}」的個股`; return; }
    $("#q-stock").value = "";
    if (sid === CUR_SID) return;
    CUR_SID = sid;
    loadStock(true);
  };
  $("#q-stock").onchange = onPickSid;
  $("#q-stock").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); onPickSid(); }
  });
  $("#s-from").onchange = () => loadStock();
  $("#s-to").onchange = () => loadStock();
  $("#s-topn").onchange = () => { loadStock(); loadStockPnl(); };
  document.querySelectorAll("#s-days button[data-sq]").forEach(b => {
    b.onclick = () => { setStockQuick(+b.dataset.sq); loadStock(); };
  });
  const showTab = which => {
    $("#s-pair").style.display = "none";
    ["b","s","e","m","d"].forEach(k=>{
      $("#view-"+k).style.display = k===which ? "" : "none";
      $("#tab-"+k).className = k===which ? "on" : "";
    });
  };
  $("#tab-b").onclick = ()=> showTab("b");
  $("#tab-s").onclick = async ()=>{ showTab("s");
    if (!S_DATES.length) await loadStock(true); };
  $("#tab-e").onclick = async ()=>{ showTab("e"); await initEtf(); };
  $("#tab-m").onclick = async ()=>{ showTab("m");
    if (!M_DATA) await loadIndex(); };
  $("#tab-d").onclick = async ()=>{ showTab("d"); await initDemo(); };
  document.querySelectorAll("#m-days button[data-mq]").forEach(b=>{
    b.onclick = () => { M_DAYS = +b.dataset.mq;
      markQuick("#m-days", M_DAYS); loadIndex(); };
  });
  $("#m-fut-c").onchange = renderIndex;
  $("#m-lt-c").onchange = renderIndex;
  $("#e-from").onchange = () => { markQuick("#e-days",-1); loadConsensus(); };
  $("#e-to").onchange   = () => { markQuick("#e-days",-1); loadConsensus(); };
  $("#e-topn").onchange = () => loadConsensus();
  $("#e-etf").onchange  = () => { $("#e-date").value = ""; loadFund(); };
  $("#e-date").onchange = () => loadFund();
  $("#e-date-latest").onclick = () => { $("#e-date").value = ""; loadFund(); };
  document.querySelectorAll("#e-days button[data-eq]").forEach(b=>{
    b.onclick = () => { setEtfQuick(+b.dataset.eq); loadConsensus(); };
  });
}
init().then(() => {
  // 深層連結:?bno=1440&sid=2330 直接開啟該組合的 K 線與進出,方便分享
  const q = new URLSearchParams(location.search);
  const bno = q.get("bno"), sid = q.get("sid");
  if (bno && sid) showPair(bno, sid);
}).catch(e=>{ $("#meta").textContent="錯誤:"+e.message; });