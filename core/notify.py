"""LINE 群組推播(Messaging API)。各資料源提供 build_message(conn) → (text, sig)。

設定檔 line_config.json(gitignore,勿入版控),token 兩種擇一：
    {"channel_access_token": "<長期權杖>", ...}
    {"channel_id": "<Channel ID>", "channel_secret": "<Channel secret>", ...}
後者每次推播自動換發 stateless token(Basic settings 分頁就有這兩個值)。

推播目標：
    {..., "to": "<預設群組>", "targets": {"active_etf": "C...", "futures": "C..."}}
targets 未指定的來源用 to 當預設。

去重：狀態記在 data/notify_state.json,以「來源名稱」為 key,
同一來源的簽章(sig)未變就不重發,所以 18:00 與 21:30 兩個排程時段不會重複。
"""
import json
from pathlib import Path

import requests

from core import store

CONFIG = Path(__file__).parent.parent / "line_config.json"
STATE = store.BASE / "notify_state.json"


def load_config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else None


def _token(cfg):
    if cfg.get("channel_access_token"):
        return cfg["channel_access_token"]
    r = requests.post(
        "https://api.line.me/oauth2/v3/token",
        data={"grant_type": "client_credentials",
              "client_id": cfg["channel_id"],
              "client_secret": cfg["channel_secret"]},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"LINE 換發 token 失敗 {r.status_code}: {r.text}")
    return r.json()["access_token"]


def _target(cfg, source):
    return cfg.get("targets", {}).get(source) or cfg.get("to")


def push(token, to, text):
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"to": to, "messages": [{"type": "text", "text": text[:4900]}]},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"LINE push 失敗 {r.status_code}: {r.text}")


def _load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def notify(cfg, source, text, sig, dry_run=False):
    """推播單一來源的訊息，回傳是否實際送出。dry_run 只印不發、不受去重限制。"""
    if not text:
        print(f"[notify] {source}: 無內容，跳過")
        return False
    if dry_run:
        print(text)
        return False
    state = _load_state()
    if state.get(source) == sig:
        print(f"[notify] {source}: 資料未更新，跳過推播")
        return False
    push(_token(cfg), _target(cfg, source), text)
    state[source] = sig
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    print(f"[notify] {source}: 已推播（{len(text)} 字）")
    return True
