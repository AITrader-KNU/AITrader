"""DB 스키마가 만들어지고 테이블 7개가 있는지 확인. 실행: pytest"""
from common import db

EXPECTED = {"daily_candles", "news", "candidates", "decisions",
            "sell_requests", "positions_meta", "orders_log"}


def test_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init_db()
    with db.connect() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED <= names


def test_request_status_default(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init_db()
    with db.connect() as conn:
        conn.execute("INSERT INTO sell_requests (created_at, symbol, why) VALUES ('2026-11-02T10:00:00+09:00', '005930', 'STOP_LOSS')")
        row = conn.execute("SELECT status FROM sell_requests").fetchone()
    assert row["status"] == db.STATUS_NEW


def test_orders_log_has_fee_tax(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init_db()
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders_log)")}
    assert {"fee", "tax", "total_amount"} <= cols


def test_sell_request_rejects_bad_news(tmp_path, monkeypatch):
    """악재 매도는 decisions 의 SELL 로 처리하므로 sell_requests 에는 들어갈 수 없다."""
    import sqlite3
    import pytest
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init_db()
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn:
            conn.execute("INSERT INTO sell_requests (created_at, symbol, why) VALUES ('t', '005930', 'BAD_NEWS')")
