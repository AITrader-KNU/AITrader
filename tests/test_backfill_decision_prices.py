"""판단 시점 가격 복원 테스트. AQuA 1분봉은 가짜 함수로 바꾼다.
실행: python -m pytest tests/test_backfill_decision_prices.py -v
"""
import sys
from datetime import datetime, timedelta

import pytest

from common import db
from tools import backfill_decision_prices as bf

START = datetime.fromisoformat("2026-11-02T09:00:00+09:00")


def bar(i):
    t = START + timedelta(minutes=i)
    return {"timestamp": t.isoformat(timespec="milliseconds"), "closePrice": 10_000 + i}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init_db()
    calls = []
    allbars = [bar(i) for i in range(400)]          # 09:00 ~ 15:39, 최신부터 200개씩

    def get_candles(symbol, interval, count, before=None):
        calls.append(before)
        end = len(allbars) if before is None else int(before)
        page = allbars[max(0, end - count):end]
        return {"candles": list(reversed(page)), "nextBefore": str(end - count) if end - count > 0 else None}

    monkeypatch.setattr(bf.aqua_client, "get_candles", get_candles)
    return calls


def add_decision(symbol, run_at):
    with db.connect() as conn:
        return conn.execute("INSERT INTO decisions (run_at, symbol, action) VALUES (?,?,'WAIT')",
                            (run_at, symbol)).lastrowid


def stored():
    with db.connect() as conn:
        return {r["decision_id"]: (r["price"], r["price_time"]) for r in conn.execute("SELECT * FROM decision_prices")}


def test_price_is_close_of_last_finished_bar(fake, monkeypatch):
    a = add_decision("000001", "2026-11-02T09:30:05+09:00")   # 09:29 봉(09:30 에 끝남)이 마지막 완료 봉
    b = add_decision("000001", "2026-11-02T09:00:30+09:00")   # 끝난 봉이 없음 → 못 채움
    monkeypatch.setattr(sys, "argv", ["x"])
    bf.main()
    got = stored()
    assert got[a] == (10_029, "2026-11-02T09:29:00.000+09:00")
    assert b not in got


def test_pages_back_until_earliest_decision(fake, monkeypatch):
    add_decision("000001", "2026-11-02T09:10:00+09:00")       # 첫 페이지(200~399분)보다 과거 → 두 번째 페이지 필요
    monkeypatch.setattr(sys, "argv", ["x"])
    bf.main()
    assert len(fake) == 2


def test_dry_run_writes_nothing_and_existing_rows_are_kept(fake, monkeypatch):
    a = add_decision("000001", "2026-11-02T13:00:00+09:00")
    with db.connect() as conn:
        conn.execute("INSERT INTO decision_prices VALUES (?, 1, 't', 'live')", (a,))
    add_decision("000002", "2026-11-02T13:00:00+09:00")
    monkeypatch.setattr(sys, "argv", ["x", "--dry-run"])
    bf.main()
    assert stored() == {a: (1, "t")}
