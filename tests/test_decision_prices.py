"""판단 시점 가격 기록 테스트. AQuA 현재가는 가짜 함수로 바꾼다.
실행: python -m pytest tests/test_decision_prices.py -v
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from common import db
from order_manager import decision_prices

KST = ZoneInfo("Asia/Seoul")
T_0931 = datetime(2026, 11, 5, 9, 31, tzinfo=KST)


@pytest.fixture
def prices(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init_db()
    calls = []

    def fake_get_prices(symbols):
        calls.append(list(symbols))
        return [{"symbol": s, "price": 10_000 + i, "timestamp": "2026-11-05T09:30:00+09:00"}
                for i, s in enumerate(symbols) if s != "MISSING"]

    monkeypatch.setattr(decision_prices.aqua_client, "get_prices", fake_get_prices)
    return calls


def add_decision(symbol, action="WAIT", run_at="2026-11-05T09:30:00+09:00"):
    with db.connect() as conn:
        return conn.execute("INSERT INTO decisions (run_at, symbol, action) VALUES (?,?,?)",
                            (run_at, symbol, action)).lastrowid


def recorded():
    with db.connect() as conn:
        return {r["decision_id"]: r["price"] for r in conn.execute("SELECT * FROM decision_prices")}


def test_records_buy_and_wait_once(prices):
    a, b = add_decision("000001", "BUY"), add_decision("000002", "WAIT")
    assert decision_prices.run(T_0931) == 2
    assert set(recorded()) == {a, b}
    assert decision_prices.run(T_0931) == 0          # 이미 기록한 판단은 다시 조회하지 않음
    assert len(prices) == 1


def test_skips_other_days_and_missing_quote(prices):
    add_decision("000001", run_at="2026-11-04T13:00:00+09:00")
    c = add_decision("MISSING")
    d = add_decision("000003")
    assert decision_prices.run(T_0931) == 1
    assert set(recorded()) == {d} and c not in recorded()


def test_api_error_does_not_raise(prices, monkeypatch):
    add_decision("000001")

    def boom(symbols):
        raise RuntimeError("server down")

    monkeypatch.setattr(decision_prices.aqua_client, "get_prices", boom)
    assert decision_prices.run(T_0931) == 0
    assert recorded() == {}


def test_judge_job_records_prices_before_orders(monkeypatch):
    import main
    order = []
    monkeypatch.setattr(main.collector, "collect_news", lambda: order.append("news"))
    monkeypatch.setattr(main.judge, "run", lambda: order.append("judge"))
    monkeypatch.setattr(main.temp_rule, "run", lambda: order.append("judge"))
    monkeypatch.setattr(main.decision_prices, "run", lambda: order.append("prices"))
    monkeypatch.setattr(main.manager, "run", lambda: order.append("orders"))
    main.judge_job()
    assert order == ["news", "judge", "prices", "orders"]
