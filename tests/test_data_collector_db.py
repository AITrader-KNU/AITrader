"""기존 DB 공통 함수로 데이터 담당의 읽기·쓰기·롤백 계약을 검증한다."""
import pytest

from common import db


def test_existing_db_interface_commits_and_preserves_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "data.db")
    db.init_db()
    with db.connect() as conn:
        conn.execute("INSERT INTO daily_candles(symbol,date,close) VALUES (?,?,?)",
                     ("005930", "2026-10-22", 100))
    db.init_db()
    with db.connect() as conn:
        row = conn.execute("SELECT symbol,close FROM daily_candles WHERE symbol=?", ("005930",)).fetchone()
        assert row["symbol"] == "005930"
        assert row["close"] == 100
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_existing_db_interface_rolls_back_failed_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "data.db")
    db.init_db()
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            conn.execute("INSERT INTO daily_candles(symbol,date) VALUES (?,?)", ("005930", "2026-10-22"))
            raise RuntimeError("failed batch")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_candles").fetchone()[0] == 0
