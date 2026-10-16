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
