import sqlite3
from contextlib import contextmanager

from common.config import ROOT, get_config

STATUS_NEW = "NEW"
STATUS_DONE = "DONE"
STATUS_SKIPPED = "SKIPPED"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_candles (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        INTEGER,
    high        INTEGER,
    low         INTEGER,
    close       INTEGER,
    volume      INTEGER,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS news (
    news_id      TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL,
    title        TEXT NOT NULL,
    published_at TEXT NOT NULL,
    source       TEXT,
    url          TEXT,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_symbol_time ON news (symbol, published_at);

CREATE TABLE IF NOT EXISTS candidates (
    run_date      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    name          TEXT,
    momentum_rank INTEGER,
    ret_5d        REAL,
    above_ma20    INTEGER,
    PRIMARY KEY (run_date, symbol)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('BUY', 'WAIT', 'SELL')),
    bad_news    INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    risks       TEXT,
    news_ids    TEXT,
    status      TEXT NOT NULL DEFAULT 'NEW'
);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions (status);

CREATE TABLE IF NOT EXISTS sell_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    why        TEXT NOT NULL CHECK (why IN ('STOP_LOSS', 'TAKE_PROFIT', 'EXPIRED')),

    status     TEXT NOT NULL DEFAULT 'NEW'
);
CREATE INDEX IF NOT EXISTS idx_sell_requests_status ON sell_requests (status);

CREATE TABLE IF NOT EXISTS positions_meta (
    symbol      TEXT PRIMARY KEY,
    bought_at   TEXT NOT NULL,
    buy_price   INTEGER,
    reason      TEXT,
    decision_id INTEGER REFERENCES decisions (decision_id)
);

CREATE TABLE IF NOT EXISTS orders_log (
    order_id       INTEGER PRIMARY KEY,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity       INTEGER NOT NULL,
    status         TEXT,
    executed_price INTEGER,
    total_amount   INTEGER,
    fee            INTEGER,
    tax            INTEGER,
    source         TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_prices (
    decision_id INTEGER PRIMARY KEY REFERENCES decisions (decision_id),
    price       INTEGER,
    price_time  TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corp_codes (
    symbol      TEXT PRIMARY KEY,
    corp_code   TEXT NOT NULL,
    corp_name   TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disclosures (
    rcept_no     TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL,
    report_nm    TEXT NOT NULL,
    rcept_dt     TEXT NOT NULL,         -- YYYY-MM-DD (DART YYYYMMDD를 정규화)
    collected_at TEXT NOT NULL          -- 최초 관측 ISO 8601 KST, 마이크로초 보존
);
CREATE INDEX IF NOT EXISTS idx_disclosures_symbol_date
    ON disclosures (symbol, rcept_dt, collected_at);

CREATE TABLE IF NOT EXISTS market_regime (
    run_date         TEXT PRIMARY KEY,
    regime           TEXT NOT NULL CHECK (regime IN ('up', 'sideways', 'down')),
    index_close      REAL,
    ma_short         REAL,
    ma_long          REAL,
    max_equity_ratio REAL NOT NULL,
    created_at       TEXT NOT NULL
);
"""


def db_path():
    return ROOT / get_config()["db"]["path"]


@contextmanager
def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


if __name__ == "__main__":
    init_db()
    print(f"DB 준비 완료: {db_path()}")
