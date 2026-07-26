"""共用 SQLite 連線與原始檔落地。各資料源的建表(init)由 main.py 統一呼叫。"""
import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent.parent / "data"
DB_PATH = BASE / "market.db"
REPORTS = Path(__file__).parent.parent / "reports"


def now() -> str:
    """入庫用時間戳(fetched_at)。"""
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    BASE.mkdir(exist_ok=True)
    return sqlite3.connect(DB_PATH)


def save_raw(source: str, data_date: str, name: str, ext: str, content: bytes):
    """原始回應落檔 data/raw/<source>/<data_date>/<name>.<ext>，供重新解析備查。"""
    d = BASE / "raw" / source / data_date
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.{ext}").write_bytes(content)
