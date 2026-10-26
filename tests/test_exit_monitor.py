import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from common import db as dbm
from exit_monitor import monitor as em

KST = ZoneInfo("Asia/Seoul")
FRI = datetime(2026, 10, 23, 10, 0, tzinfo=KST)      # 금
NEXT_MON = datetime(2026, 10, 26, 10, 0, tzinfo=KST)


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    monkeypatch.setattr(dbm, "db_path", lambda: p)
    dbm.init_db()
    return p


def pf(*hs):
    return {"holdings": [dict(symbol=s, quantity=10, availableQuantity=10, avgBuyPrice=10000, currentPrice=c) for s, c in hs]}


def meta(path, **kw):
    with sqlite3.connect(path) as c:
        for s, b in kw.items():
            c.execute("INSERT INTO positions_meta (symbol, bought_at) VALUES (?,?)", (s, b))


def rows(path):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT created_at, symbol, why, status FROM sell_requests ORDER BY request_id").fetchall()


LIM = {"cooldowns": []}
BOUGHT_TODAY = "2026-10-23T09:30:00+09:00"


@pytest.mark.parametrize("cur,why", [
    (9600, "STOP_LOSS"),    # 정확히 -4%
    (9599, "STOP_LOSS"),
    (9601, None),
    (10600, "TAKE_PROFIT"), # 정확히 +6%
    (10599, None),
    (10000, None),
])
def test_boundaries(path, cur, why):
    meta(path, A=BOUGHT_TODAY)
    out = em.run_once(FRI, pf(("A", cur)), LIM)
    assert [o["why"] for o in out] == ([why] if why else [])


def test_row_format(path):
    meta(path, A=BOUGHT_TODAY)
    em.run_once(FRI, pf(("A", 9000)), LIM)
    assert rows(path) == [("2026-10-23T10:00:00+09:00", "A", "STOP_LOSS", "NEW")]


def test_expiry_five_trading_days(path):
    meta(path, A="2026-10-19T09:30:00+09:00")  # 월 매수
    assert em.run_once(datetime(2026, 10, 23, 10, tzinfo=KST), pf(("A", 10000)), LIM) == []   # 4거래일째
    out = em.run_once(NEXT_MON, pf(("A", 10000)), LIM)                                      # 5거래일째
    assert [o["why"] for o in out] == ["EXPIRED"]


def test_expiry_skips_weekend_and_holiday(path, monkeypatch):
    cfg = em.get_config()
    monkeypatch.setitem(cfg["schedule"], "holidays", ["2026-10-21"])
    meta(path, A="2026-10-19T09:30:00+09:00")
    assert em.holding_days("2026-10-19T09:30:00+09:00", NEXT_MON, cfg) == 4  # 수요일 휴장


def test_expiry_utc_bought_at(path):
    # UTC 표기여도 KST 날짜로 계산 (UTC 2026-10-18 16:00 = KST 10-19 01:00 → 월요일)
    cfg = em.get_config()
    assert em.holding_days("2026-10-18T16:00:00+00:00", NEXT_MON, cfg) == 5


def test_priority_stop_over_expiry(path):
    meta(path, A="2026-10-16T09:30:00+09:00")
    assert [o["why"] for o in em.run_once(NEXT_MON, pf(("A", 9000)), LIM)] == ["STOP_LOSS"]


def test_missing_meta_still_checks_stop(path):
    assert [o["why"] for o in em.run_once(FRI, pf(("A", 9000)), LIM)] == ["STOP_LOSS"]
    assert em.run_once(FRI, pf(("B", 10000)), LIM) == []


def test_no_duplicates(path):
    meta(path, A=BOUGHT_TODAY)
    em.run_once(FRI, pf(("A", 9000)), LIM)
    em.run_once(FRI, pf(("A", 9000)), LIM)
    assert len(rows(path)) == 1
    with sqlite3.connect(path) as c:           # 주문 관리자가 처리(DONE)한 뒤 아직 보유 중이면 다시 가능
        c.execute("UPDATE sell_requests SET status='DONE'")
    em.run_once(FRI, pf(("A", 9000)), LIM)
    assert len(rows(path)) == 2


def test_cooldown_holds_then_releases(path):
    meta(path, A=BOUGHT_TODAY)
    lim = {"cooldowns": [{"symbol": "A", "tradableAt": "2026-10-23T10:20:00+09:00"}]}
    assert em.run_once(FRI, pf(("A", 9000)), lim) == [] and rows(path) == []
    later = datetime(2026, 10, 23, 10, 30, tzinfo=KST)
    assert [o["why"] for o in em.run_once(later, pf(("A", 9000)), lim)] == ["STOP_LOSS"]
    assert len(rows(path)) == 1


def test_cooldown_z_suffix(path):
    meta(path, A=BOUGHT_TODAY)
    lim = {"cooldowns": [{"symbol": "A", "tradableAt": "2026-10-23T01:20:00Z"}]}  # = KST 10:20
    assert em.run_once(FRI, pf(("A", 9000)), lim) == []


def test_zero_available_quantity_held(path):
    meta(path, A=BOUGHT_TODAY)
    p = pf(("A", 9000)); p["holdings"][0]["availableQuantity"] = 0
    assert em.run_once(FRI, p, LIM) == []


def test_bad_prices_ignored(path):
    p = pf(("A", 0)); p["holdings"].append(dict(symbol="B", quantity=1, availableQuantity=1, avgBuyPrice=0, currentPrice=100))
    assert em.run_once(FRI, p, LIM) == []


def test_multiple_symbols(path):
    meta(path, A=BOUGHT_TODAY, B=BOUGHT_TODAY, C=BOUGHT_TODAY)
    out = em.run_once(FRI, pf(("A", 9000), ("B", 11000), ("C", 10100)), LIM)
    assert {(o["symbol"], o["why"]) for o in out} == {("A", "STOP_LOSS"), ("B", "TAKE_PROFIT")}


def test_in_market():
    cfg = em.get_config()
    assert em.in_market(FRI, cfg)
    assert not em.in_market(datetime(2026, 10, 23, 15, 0, tzinfo=KST), cfg)
    assert not em.in_market(datetime(2026, 10, 24, 10, 0, tzinfo=KST), cfg)  # 토
