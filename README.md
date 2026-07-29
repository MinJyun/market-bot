# market-bot

排程抓取台股市場數據 → 存 SQLite → 推 LINE 群組。外掛式架構:每種數據是
`sources/` 下的一個模組,共用排程、儲存與推播;要加新數據就新增一個模組。

## 架構

```
market-bot/
├── main.py              # 調度:遍歷啟用的來源 → fetch / report / notify
├── daily.sh            # 排程入口(launchd 呼叫):daily + 自動 commit/push
├── core/
│   ├── store.py         # 共用 SQLite 連線 + 原始檔落地
│   └── notify.py        # LINE 推播 + 去重(以來源為 key)
├── sources/
│   └── active_etf.py    # 資料源:台股主動式 ETF 每日持股
├── data/
│   ├── market.db        # SQLite(各來源各自建表)
│   └── raw/<來源>/<資料日>/   # 原始回應,供重新解析備查
├── reports/<資料日>/     # 各來源產出的 Markdown 報告
└── line_config.json     # LINE 憑證與推播目標(gitignore,勿入版控)
```

**資料源契約**:每個 `sources/*.py` 模組提供 `NAME`、`init(conn)`(建表,
由 main.py 統一呼叫)、`fetch(conn)`、`build_message(conn) → (文字, 簽章)`,
選配 `backfill(conn, days)`、`report(conn)`。在 `main.py` 的 `SOURCES` 登記後
即納入排程。TWSE 逐日型來源的抓取迴圈共用 `core/twse.py`;期交所頁面的抓取
與表格解析共用 `core/taifex.py`。

## 用法

```bash
python3 main.py daily                  # 抓取 + 報告 + 推 LINE(排程用)
python3 main.py fetch                   # 只抓最新入庫
python3 main.py backfill --days 10      # 回補歷史(支援的來源)
python3 main.py report                  # 只產報告
python3 main.py notify [--dry-run]      # 只推 LINE(dry-run 只印不發)
python3 main.py fetch --source active_etf   # 只處理指定來源(可重複)
```

排程:`~/Library/LaunchAgents/com.minjyun.market-bot.plist`,週一至週五
18:00 與 21:30(備援)各跑一次 `daily.sh`,手動觸發:
`launchctl kickstart gui/$(id -u)/com.minjyun.market-bot`。

## 資料源:active_etf(台股主動式 ETF)

| ETF | 名稱 | 來源 | 歷史回補 |
|---|---|---|---|
| 00981A | 主動統一台股增長 | 統一 GetPCF API (JSON) | 可 |
| 00403A | 主動統一升級50 | 統一 GetPCF API (JSON) | 可 |
| 00988A | 主動統一全球創新 | 統一 GetPCF API (JSON) | 可 |
| 00991A | 主動復華未來50 | 復華 assetsExcel API (xlsx) | 可 |
| 00990A | 主動元大AI新經濟 | 元大 bridge API (JSON) | 不可(僅最新一日,斷抓即缺) |
| 00992A | 主動群益科技創新 | 群益 CFWeb buyback API (JSON) | 暫不(未驗證) |
| 00982A | 主動群益台灣強棒 | 群益 CFWeb buyback API (JSON) | 暫不(未驗證) |

> 群益 capitalfund.com.tw 在 Imperva WAF 後面,標準 requests 的 TLS 指紋被擋,
> 改用 `curl_cffi` 模擬 Chrome 才能抓;所以本專案依賴 `curl_cffi`(見 requirements.txt)。
> 野村 00980A(F5 BigIP,對非瀏覽器發圖片挑戰)尚未納入,需 headless browser。

判讀注意:

- 各家揭露延遲不同(統一當日、復華 T、元大約 T-2),diff 以「資料日」對齊。
- 五檔皆為現金申購/買回:申贖進出的是現金、不直接改變持股,所以股數差就是
  經理人實際買賣,不做規模校正。流通單位數變化列在報告開頭供判讀。
- 除權息、股票分割造成的股數跳動無法自動辨識,遇到異常大的變化請對照原始檔。
- 來源皆為未公開的官網內部 API,投信改版即失效;抓取失敗會印錯誤並以非零
  exit code 結束。

## 資料源:market_index(加權指數收盤行情與成交金額)

TWSE FMTQIK「每日市場成交資訊」:大盤收盤指數、漲跌點數、成交金額。放在
「法人籌碼」訊息最前面,作為當天其他籌碼指標的漲跌與量能對照基準(縮量跌
與爆量跌判讀不同)。漲跌百分比由點數回推(API 未提供)。一次請求回傳整月,
fetch 順帶回補當月;backfill 依月往前。TWSE 開放 JSON、無 bot 防護。

## 資料源:macro(國際總經)

台股籌碼的國際對照基準,一次呈現九個序列:

| 序列 | 來源 |
|---|---|
| 美元/台幣、美元/日圓 | 期交所每日外幣參考匯率(curl_cffi,預設頁含兩月歷史) |
| 美元指數、WTI原油、黃金、美債10Y、VIX、費半、S&P500 | Yahoo Finance chart API(免金鑰,每次抓近一月日線) |

美股相關數值為台北時間清晨的美國收盤,與台股資料日相差一天屬正常(訊息
標注)。fetch 即回補,不需 backfill。前身為只有美元/台幣的 fx 來源。

## 資料源:inst_otc(三大法人上櫃買賣超)

TPEx 櫃買中心「三大法人買賣金額彙總表」:外資/投信/自營每日在上櫃市場的
買賣超金額。與 inst_spot(上市)互補——中小型股籌碼主要在上櫃。TPEx 開放
JSON、無 bot 防護,可回補歷史。

## 資料源:futures_traders(期貨大額交易人)

期交所每日「期貨大額交易人未沖銷部位」的**五大/十大交易人(特定法人)**淨部位,
追蹤台指期、電子期、金融期三檔股價指數期貨的「所有契約」列。訊息顯示每檔的
未平倉口數、前五大與前十大特定法人淨部位(淨多/淨空)及前日變化。

- 來源:`taifex.com.tw/cht/3/largeTraderFutQry`,同群益需 `curl_cffi` 破 bot 防護。
- 每格數字為「全部交易人(其中特定法人)」,取特定法人;淨部位 = 買方 − 賣方。
- backfill 只回補「三大法人淨部位」(POST queryDate,期交所僅開放**最近 30 個
  交易日**)與「台指期近月結算價」(futDataDown CSV,僅最近 3 個月);
  大額交易人仍僅當日頁可解析、不可回補。下載中心(futContractsDateDown)在
  Cloudflare 挑戰後面,已試多種 TLS 指紋皆 429,不硬闖。

**外資台指期成本估算**:逐日以「淨部位增量 × 當日近月結算價」累積——加碼攤入
成本、減碼不動、翻向以當日價重置;浮動損益 =(結算 − 成本)× 淨口數 × 200 元。
**這是估算不是真值**:期交所只公布按結算價計的未平倉市值,不公布成本;盤中
成交價、跨月轉倉價差、價差單皆無法反映,且起算日受 30 交易日限制(訊息中
標注「自MM/DD累計」),隨每日累積會自然加深。趨勢參考用。

## 資料源:options_traders(臺指選擇權機構籌碼)

期交所臺指選擇權(TXO),同時呈現三張表:

- **三大法人未平倉淨額**(外資/投信/自營):`optContractsDateExcel`,不分買賣權
  方向(多空淨額 = Call 淨 − Put 淨,與買賣權分計可互相驗證)。
- **三大法人買賣權分計**:`callsAndPutsDateExcel`,依 Call/Put 拆分的未平倉
  淨口數——外資「買 Put 避險 vs 買 Call 追多」的主要判讀。
- **大額交易人前五/十大(特定法人)**:`largeTraderOptQry`,分買權(Call)、
  賣權(Put)分別呈現買方/賣方部位(方向意義相反,不併成單一淨部位)。

皆需 `curl_cffi` 破 bot 防護。**不做 backfill**(理由同 futures_traders:大額
交易人僅當日預設頁可穩定解析),歷史從今天起每日累積。

## 資料源:inst_spot(三大法人現貨買賣超)

TWSE「三大法人買賣金額統計表」(BFI82U):外資/投信/自營每日在上市現貨市場的
買賣超金額(正=買超、負=賣超)。與期貨外資淨部位互補——現貨買+期貨空常是避險,
現貨賣+期貨空才是真看空。TWSE 開放 JSON、無 bot 防護,可回補歷史。

## 資料源:inst_stock(個股籌碼,獨立一則 LINE)

抓取:TWSE「三大法人買賣超日報」(T86)依個股的外資/投信/自營/合計買賣超
股數,只保留純股票(4 碼純數字、非 00 開頭)入庫,約 1000 檔。TWSE 開放
JSON、可回補歷史。

訊息:獨立成「個股籌碼」一則(GROUPS 的 stocks 分組),**上市/上櫃分開**,
每市場列 **三大法人/外資/投信** 各自的買超前5、賣超前5;連續同向 ≥2 天標注
「連N買/連N賣」(投信連買常被視為認養訊號)。上櫃逐股資料讀 inst_otc 入庫
的 `inst_otc_stock` 表。最後保留 **ETF 關注股**:主動 ETF 目前持股(上市+
上櫃)∩ 法人買賣超前 5,對照經理人加減碼是否與法人同向。

## 資料源:margin(融資融券餘額 + 借券賣出)

兩個 TWSE endpoint 合成一則「槓桿/放空全貌」訊息,皆開放 JSON、可回補歷史:

- MI_MARGN「信用交易統計」彙總表:全市場融資、融券每日餘額(張)與較前日
  增減,另取「融資金額(仟元)」看散戶槓桿的資金水位(訊息以億顯示)。
- TWT93U「融券借券賣出餘額」:逐股加總全市場借券賣出餘額(股,訊息換算張)。
  外資放空主要走借券而非融券,補融券看不到的放空力道。

> 口徑注意:TWT93U 的「融券」欄位逐股加總恰為 MI_MARGN 全市場融券張數的
> 2 倍(彙總口徑不同),故融券數字一律以 MI_MARGN 為準;本專案只取 TWT93U
> 的借券賣出欄位(個股數值已對照台積電/力積電等驗證單位為股)。

除彙總外,逐股資券/借券餘額也入庫(`margin_stock` 表,同一回應、零額外
請求),供 my_chips 交叉個人持股。

## 資料源:my_chips(我的持股籌碼 → Google Sheets)

從 trade-sync 專案每日寫入 Google Sheets 的「每日持股 YYYY」tab 讀持股清單,
交叉本專案已入庫的個股籌碼,把逐股籌碼變化 append 到**同一本試算表**的
「持股籌碼 YYYY」tab(分年度、自動建立、同日不重寫):

| 欄位 | 上市來源 | 上櫃來源(fallback) |
|---|---|---|
| 外資/投信/自營/合計買賣超(張) | inst_stock(TWSE T86) | inst_otc_stock(TPEx dailyTrade) |
| 融資/融券餘額與增減(張) | margin_stock(TWSE MI_MARGN) | margin_otc_stock(TPEx balance) |
| 借券賣出餘額與增減(張) | margin_stock(TWSE TWT93U) | margin_otc_stock(TPEx sbl) |

- 憑證與試算表 ID 直接沿用 `../trade-sync/.env`(`GOOGLE_SERVICE_ACCOUNT_JSON`、
  `GOOGLE_SHEET_ID`),不另存一份;依賴 `gspread`。
- fetch 匯入整份持股歷史(冪等),report 對每個有籌碼的交易日取「該日以前
  最近一天」的持股組列;tab 為純衍生資料,砍掉重跑 report 即可全量重建。
- **個人持股不進 LINE 推播**(訊息會被轉發),`build_message` 恆回 None。
- 另在「每日持股 YYYY」的 J 欄之後放 ARRAYFORMULA(以 日期|股名 VLOOKUP
  持股籌碼 tab),同一畫面看帳務與籌碼;公式由 report 冪等維護(跨年自動
  對新 tab 補設),trade-sync append 的新列會自動帶出(已用複本實測不影響
  其 append 落點)。J~S 欄需設一般數字格式,否則會繼承 I 欄的百分比格式。
- 排程相依:trade-sync 16:05 寫持股快照,本專案 18:00 daily 讀,同日資料齊。

## 資料源:pc_ratio(選擇權 Put/Call Ratio)

期交所臺指選擇權(TXO)每日賣權/買權的成交量比與未平倉量比(%)——市場情緒
指標,比率越高代表賣權相對買權越多。

- 來源:`taifex.com.tw/cht/3/pcRatio`,同群益需 `curl_cffi` 破 bot 防護。
- 頁面預設(免帶查詢參數)即回傳約一個月的交易日歷史表,`fetch` 每次執行就
  順帶回補近況,不需另外實作 `backfill`。

## 每日彙整與用量

- `main.py` 每次 `notify`/`daily` 會寫 `reports/<本機當天日期>/daily.md`,收錄當天
  各來源的訊息內容,本機留存。
- 每次 `notify`/`daily` 結尾印 `[quota] 本月已通知 X / 200 封 LINE`(查 LINE 實際
  額度),避免超過免費 200 封/月。

## LINE 推播設定

`line_config.json`(gitignore),token 兩種擇一,並指定推播目標:

```json
{
  "channel_access_token": "<長期權杖>",
  "to": "<預設群組ID C開頭>",
  "targets": {"active_etf": "<可指定該來源專屬群組>"}
}
```

或用 `channel_id` + `channel_secret`(Basic settings 分頁就有,自動換發短效
token)。`targets` 未指定的來源用 `to` 當預設。去重狀態在
`data/notify_state.json`,同一來源簽章未變不重發。
