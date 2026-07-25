"""共用 SQLite 連線與原始檔落地。各資料源自行建表(呼叫自己的 init)。"""
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent.parent / "data"
DB_PATH = BASE / "market.db"


def connect() -> sqlite3.Connection:
    BASE.mkdir(exist_ok=True)
    return sqlite3.connect(DB_PATH)


def save_raw(source: str, data_date: str, name: str, ext: str, content: bytes):
    """原始回應落檔 data/raw/<source>/<data_date>/<name>.<ext>，供重新解析備查。"""
    d = BASE / "raw" / source / data_date
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.{ext}").write_bytes(content)
