"""市場數據追蹤:排程抓取 → 存 → 推 LINE。

每個資料源是 sources/ 下的一個模組,實作 fetch/build_message
(及選配的 backfill/report)。資料源依主題分組(GROUPS),每組合併成「一則」
LINE 推播:ETF 持股獨立一則、法人籌碼(現貨→期貨→選擇權)合併一則。
daily 會先把所有來源抓齊,才組訊息推播。

用法:
    python3 main.py fetch                  # 抓各來源最新資料入庫
    python3 main.py backfill --days 10     # 回補歷史(支援的來源)
    python3 main.py report                 # 產出報告(支援的來源)
    python3 main.py notify [--dry-run]     # 推 LINE(dry-run 只印不發)
    python3 main.py daily                  # fetch + report + notify + 當日 md(排程用)
    --source active_etf                    # 只處理指定來源(可重複)
"""
import argparse
import sys
from datetime import date, datetime

from core import notify as notifier
from core import store
from sources import (active_etf, fut_night, futures_traders, inst_otc,
                     inst_spot, inst_stock, macro, margin, market_index,
                     my_chips, options_traders, pc_ratio)

# (去重/推播目標 key, md 區塊標題, 成員來源)。每組合併成一則 LINE。
# my_chips 只寫 Google Sheets、不出 LINE(build_message 恆回 None)。
GROUPS = [
    ("etf", "主動ETF持股", [active_etf]),
    ("chips", "法人籌碼", [market_index, inst_spot, inst_otc, margin,
                          futures_traders, options_traders, pc_ratio,
                          my_chips]),
    ("stocks", "個股籌碼", [inst_stock]),
    ("morning", "國際總經與夜盤", [macro, fut_night]),
]
# 各組實發時窗 [起, 迄):morning 早上 8 點排程發(美股收盤+夜盤剛結束);
# chips 與 stocks 21:30 發——上櫃法人個股 18:00 尚未公布,提早發會在
# 21:30 因簽章更新而重發一次。其餘不限。md 彙整照寫、dry-run 不受限。
SEND_WINDOW = {"morning": (6, 12), "chips": (21, 24), "stocks": (21, 24)}
SOURCES = {s.NAME: s for _, _, members in GROUPS for s in members}


def _write_daily_md(sections):
    """一天一份彙整檔 reports/<本機當天日期>/daily.md,含當天全部區塊。"""
    today = date.today().isoformat()
    out = store.REPORTS / today
    out.mkdir(parents=True, exist_ok=True)
    body = [f"# 每日市場籌碼 {today}", ""]
    for title, text in sections:
        body.append(f"## {title}\n\n```\n{text}\n```\n")
    (out / "daily.md").write_text("\n".join(body), encoding="utf-8")
    print(f"[daily] 已寫入 {out / 'daily.md'}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["fetch", "backfill", "report", "notify", "daily"])
    ap.add_argument("--source", action="append", choices=list(SOURCES),
                    help="只處理指定來源(可重複),預設全部")
    ap.add_argument("--days", type=int, default=10, help="backfill 回補天數")
    ap.add_argument("--dry-run", action="store_true", help="notify 只印不發")
    args = ap.parse_args()
    only = set(args.source) if args.source else None

    conn = store.connect()
    cfg = notifier.load_config()
    failures = []

    # 統一建表:report/notify 單獨執行(或跨來源查詢)也不會缺表
    for s in SOURCES.values():
        s.init(conn)

    def active(s):
        return only is None or s.NAME in only

    # 1) 先把所有來源抓齊(發通知前確保資料完整)
    if args.command in ("fetch", "daily"):
        for s in SOURCES.values():
            if active(s):
                failures += [x if x == s.NAME else f"{s.NAME}:{x}"
                             for x in (s.fetch(conn) or [])]
    if args.command == "backfill":
        for s in SOURCES.values():
            if active(s) and hasattr(s, "backfill"):
                s.backfill(conn, args.days)
    if args.command in ("report", "daily"):
        for s in SOURCES.values():
            if active(s) and hasattr(s, "report"):
                s.report(conn)

    # 2) 分組:每組合併成一則推播
    sections = []
    if args.command in ("notify", "daily"):
        for key, title, members in GROUPS:
            parts, sig = [], {}
            for m in members:
                if not active(m):
                    continue
                text, s = m.build_message(conn)
                if text:
                    parts.append(text)
                    sig[m.NAME] = s
            if not parts:
                continue
            combined = "\n\n".join(parts)
            sections.append((title, combined))
            if cfg is None:
                if args.command == "notify":
                    print("[notify] 未設定 line_config.json,跳過推播")
            elif (not args.dry_run and key in SEND_WINDOW
                  and not (SEND_WINDOW[key][0] <= datetime.now().hour
                           < SEND_WINDOW[key][1])):
                lo, hi = SEND_WINDOW[key]
                print(f"[notify] {key}: 只在 {lo}~{hi} 點間推播,本輪保留")
            else:
                sent = notifier.notify(cfg, key, combined, sig,
                                       dry_run=args.dry_run)
                # chips 文字實發後附一張儀表板圖(同一份 DB 資料渲染);
                # 圖片失敗只印訊息,不影響文字與去重
                if key == "chips" and sent:
                    try:
                        from core import dashboard
                        dashboard.deliver(conn, cfg, notifier)
                    except Exception as e:
                        print(f"[dashboard] 失敗 — {e}")

    # 3) 當日彙整 md + 用量:只在完整 daily 執行時寫(避免被單源覆寫)
    if args.command == "daily":
        if sections:
            _write_daily_md(sections)
        if cfg is not None:
            used, limit = notifier.quota_status(cfg)
            if used is not None:
                print(f"[quota] 本月已通知 {used} / {limit} 封 LINE")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
