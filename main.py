"""市場數據追蹤:排程抓取 → 存 → 推 LINE。

每個資料源是 sources/ 下的一個模組,實作 fetch/build_message
(及選配的 backfill/report)。要加新數據(例:期貨大額交易人)就新增一個
模組並登記到 SOURCES,排程/儲存/推播共用不必重寫。

用法:
    python3 main.py fetch                  # 抓各來源最新資料入庫
    python3 main.py backfill --days 10     # 回補歷史(支援的來源)
    python3 main.py report                 # 產出報告(支援的來源)
    python3 main.py notify [--dry-run]     # 推 LINE(dry-run 只印不發)
    python3 main.py daily                  # fetch + report + notify(排程用)
    --source active_etf                    # 只處理指定來源(可重複)
"""
import argparse
import sys

from core import notify as notifier
from core import store
from sources import active_etf, futures_traders, options_traders

SOURCES = {s.NAME: s for s in [active_etf, futures_traders, options_traders]}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["fetch", "backfill", "report", "notify", "daily"])
    ap.add_argument("--source", action="append", choices=list(SOURCES),
                    help="只處理指定來源(可重複),預設全部")
    ap.add_argument("--days", type=int, default=10, help="backfill 回補天數")
    ap.add_argument("--dry-run", action="store_true", help="notify 只印不發")
    args = ap.parse_args()
    sources = [SOURCES[n] for n in (args.source or SOURCES)]

    conn = store.connect()
    cfg = notifier.load_config()
    failures = []
    for s in sources:
        if args.command in ("fetch", "daily"):
            failures += [f"{s.NAME}:{x}" for x in (s.fetch(conn) or [])]
        if args.command == "backfill" and hasattr(s, "backfill"):
            s.backfill(conn, args.days)
        if args.command in ("report", "daily") and hasattr(s, "report"):
            s.report(conn)
        if args.command in ("notify", "daily"):
            if cfg is None:
                print("[notify] 未設定 line_config.json,跳過推播")
            else:
                text, sig = s.build_message(conn)
                notifier.notify(cfg, s.NAME, text, sig, dry_run=args.dry_run)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
