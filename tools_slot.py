"""巡邏式排程的時段判斷:這次該不該跑、跑過沒。

原本用 launchd 的 StartCalendarInterval,有兩個治不好的毛病:
  1. 觸發時刻跟著**機器本地時區**跑。2026-10-05 人在 Oslo(UTC+2),台北
     8:10 的早報變成台北 14:10 才觸發,時窗判斷也跟著整組偏掉。
  2. 錯過就是錯過。機器睡著時不會觸發,只在喚醒時補跑一次,而剛喚醒
     DNS 往往還沒就緒(10-03、10-05 兩次整輪全軍覆沒都是這樣)。

改成每 30 分鐘巡邏一次,由本檔以**台北時間**判斷「今天有哪個時段已到而
還沒跑」。時區再怎麼變都無所謂;睡過頭的時段醒來後下一次巡邏就補上。

用法:
    tools_slot.py due          # 有時段該跑 → 印出 HH:MM 並 exit 0;否則 exit 1
    tools_slot.py done HH:MM   # 記錄該時段已完成
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ["TZ"] = "Asia/Taipei"   # 不靠呼叫端的環境,本檔自己釘住台北時間
time.tzset()

STATE = Path(__file__).parent / "data" / "slot_state.json"

# (時, 分, 星期集合)  星期:週一=0 … 週日=6
MON_FRI = {0, 1, 2, 3, 4}
MON_SAT = {0, 1, 2, 3, 4, 5}
SLOTS = [
    (8, 10, MON_SAT),    # 早報(週六報週五夜盤;週一/收假後為休市特別版)
    (8, 40, MON_SAT),    # 早報第二輪:8:10 資料未齊或推播失敗時補
    (15, 30, MON_FRI),   # 期貨選擇權先行版
    (16, 15, MON_FRI),   # 先行版補一輪
    (18, 0, MON_FRI),    # 主動式 ETF(此時持股才完整)
    (21, 30, MON_FRI),   # 完整籌碼 + 個股籌碼
]


def _due(now):
    """回傳今天已到而最晚的那個時段 'HH:MM';今天還沒有任何時段到則 None。"""
    passed = [(h, m) for h, m, days in SLOTS
              if now.weekday() in days and (h, m) <= (now.hour, now.minute)]
    if not passed:
        return None
    h, m = max(passed)
    return f"{h:02d}:{m:02d}"


def _load():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    now = datetime.now()
    today = now.date().isoformat()

    if cmd == "due":
        slot = _due(now)
        if not slot:
            return 1
        state = _load()
        # 跨到新的一天就重新開始;同一天只在「有更晚的時段到了」才再跑
        done = state.get("last") if state.get("date") == today else None
        if done is not None and slot <= done:
            return 1
        print(slot)
        return 0

    if cmd == "done" and len(sys.argv) > 2:
        STATE.write_text(json.dumps({"date": today, "last": sys.argv[2]},
                                    ensure_ascii=False))
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
