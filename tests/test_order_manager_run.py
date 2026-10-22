"""주문 관리자 run() 테스트. AQuA 는 가짜 클라이언트(FakeAqua)로 바꿔서 실제 주문 없이 확인한다.
실행: python -m pytest tests/test_order_manager_run.py -v
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from common import aqua_client, db
from common.aqua_client import AquaError
from order_manager import manager, temp_rule

KST = ZoneInfo("Asia/Seoul")
T_1000 = datetime(2026, 11, 2, 10, 0, tzinfo=KST)
TODAY = "2026-11-02"


class FakeAqua:
    """AQuA 대신 쓰는 가짜. 주문은 orders 에 쌓고, 시장가는 전량 체결된 것으로 응답한다."""
    DRY_RUN = False

    def __init__(self, cash=100_000_000, holdings=None, remaining=30, cooldowns=None, price=10_000):
        self.cash, self.price = cash, price
        self.holdings = holdings or []
        self.remaining, self.cooldowns = remaining, cooldowns or []
        self.orders, self.fail = [], {}      # fail[symbol] = 던질 예외
        self.next_id = 1

    def get_portfolio(self):
        value = sum(h["quantity"] * self.price for h in self.holdings)
        return {"cash": self.cash, "availableCash": self.cash, "totalAssetValue": self.cash + value,
                "holdings": self.holdings}

    def get_limit(self):
        return {"remainingTrades": self.remaining, "cooldowns": self.cooldowns}

    def get_orderbook(self, symbol):
        if symbol == "UNKNOWN":
            raise AquaError(404, "UNKNOWN_SYMBOL")
        return {"asks": [{"price": self.price, "volume": 100_000}], "bids": []}

    def get_orders(self, status=None, page=0, size=100):
        return {"orders": [o for o in self.orders if o.get("accepted")]}

    def place_order(self, symbol, side, quantity, price=None):
        if symbol in self.fail:
            raise self.fail[symbol]
        self.remaining -= 1
        amount = quantity * self.price
        resp = {"orderId": self.next_id, "symbol": symbol, "order_code": side, "quantity": quantity,
                "filledQuantity": quantity, "status": "FILLED", "executedPrice": self.price,
                "totalAmount": amount, "fee": amount * 15 // 100_000, "tax": 0 if side == "BUY" else amount // 500,
                "remainingTrades": self.remaining, "createdAt": T_1000.isoformat()}
        self.next_id += 1
        self.orders.append(resp)
        return resp


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init_db()
    fake = FakeAqua()
    monkeypatch.setattr(manager, "aqua", fake)
    return fake


def add_decision(symbol, action="BUY", bad_news=0, run_at=f"{TODAY}T09:30:00+09:00"):
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO decisions (run_at, symbol, action, bad_news, reason) VALUES (?,?,?,?,?)",
                           (run_at, symbol, action, bad_news, "test"))
        return cur.lastrowid


def add_candidate(symbol, rank):
    with db.connect() as conn:
        conn.execute("INSERT INTO candidates (run_date, symbol, momentum_rank) VALUES (?,?,?)", (TODAY, symbol, rank))


def add_sell_request(symbol, why="STOP_LOSS"):
    with db.connect() as conn:
        return conn.execute("INSERT INTO sell_requests (created_at, symbol, why) VALUES (?,?,?)",
                            (f"{TODAY}T10:00:00+09:00", symbol, why)).lastrowid


def add_position(symbol):
    with db.connect() as conn:
        conn.execute("INSERT INTO positions_meta (symbol, bought_at, buy_price) VALUES (?,?,?)",
                     (symbol, f"{TODAY}T09:30:00+09:00", 10_000))


def status(table, key, rid):
    with db.connect() as conn:
        return conn.execute(f"SELECT status FROM {table} WHERE {key} = ?", (rid,)).fetchone()["status"]


def positions():
    with db.connect() as conn:
        return {r["symbol"] for r in conn.execute("SELECT symbol FROM positions_meta")}


def held(symbol, qty=100):
    return {"symbol": symbol, "quantity": qty, "availableQuantity": qty}


# ---------- 매수 ----------
def test_buy_records_order_and_position(fake):
    did = add_decision("005930")
    manager.run(T_1000)
    assert status("decisions", "decision_id", did) == "DONE"
    assert positions() == {"005930"}
    o = fake.orders[0]
    assert o["order_code"] == "BUY" and o["quantity"] == 1_499       # 1억 × 15% ÷ 1만원, 수수료 포함
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM orders_log").fetchone()
    assert row["source"] == f"decision:{did}" and row["fee"] == o["fee"]


@pytest.mark.parametrize("case", ["held", "bad_news", "after_cutoff", "max_positions"])
def test_buy_checks_skip(fake, case):
    now = T_1000
    if case == "held":
        fake.holdings = [held("005930")]
    if case == "max_positions":
        fake.holdings = [held(f"00000{i}") for i in range(6)]
    if case == "after_cutoff":
        now = datetime(2026, 11, 2, 14, 0, tzinfo=KST)
    did = add_decision("005930", bad_news=1 if case == "bad_news" else 0)
    manager.run(now)
    assert status("decisions", "decision_id", did) == "SKIPPED"
    assert fake.orders == []


def test_buys_follow_momentum_rank(fake):
    for sym, rank in [("A", 3), ("B", 1), ("C", 2)]:
        add_candidate(sym, rank)
        add_decision(sym)
    manager.run(T_1000)
    assert [o["symbol"] for o in fake.orders] == ["B", "C", "A"]


# ---------- 매도 ----------
def test_sell_request_full_sale_removes_position(fake):
    fake.holdings = [held("005930", 50)]
    add_position("005930")
    rid = add_sell_request("005930")
    manager.run(T_1000)
    assert status("sell_requests", "request_id", rid) == "DONE"
    assert fake.orders[0]["order_code"] == "SELL" and fake.orders[0]["quantity"] == 50
    assert positions() == set()


def test_sells_before_buys(fake):
    fake.holdings = [held("111111")]
    add_decision("222222", "BUY")
    add_decision("111111", "SELL")
    add_sell_request("111111")
    manager.run(T_1000)
    assert [(o["order_code"], o["symbol"]) for o in fake.orders] == [("SELL", "111111"), ("BUY", "222222")]


def test_sell_not_held_is_skipped(fake):
    did = add_decision("005930", "SELL")
    manager.run(T_1000)
    assert status("decisions", "decision_id", did) == "SKIPPED"
    assert fake.orders == []


# ---------- 상태 정리 ----------
def test_wait_old_buy_and_duplicates_are_skipped(fake):
    wait = add_decision("A", "WAIT")
    old_buy = add_decision("B", "BUY", run_at="2026-10-30T13:00:00+09:00")
    first = add_decision("C", "BUY", run_at=f"{TODAY}T09:30:00+09:00")
    latest = add_decision("C", "BUY", run_at=f"{TODAY}T11:00:00+09:00")
    manager.run(T_1000)
    assert status("decisions", "decision_id", wait) == "SKIPPED"
    assert status("decisions", "decision_id", old_buy) == "SKIPPED"
    assert status("decisions", "decision_id", first) == "SKIPPED"
    assert status("decisions", "decision_id", latest) == "DONE"
    assert len(fake.orders) == 1


# ---------- 쿨다운·거래 횟수·오류 ----------
def test_cooldown_keeps_request_new(fake):
    fake.holdings = [held("005930")]
    fake.cooldowns = [{"symbol": "005930", "tradableAt": "2026-11-02T10:20:00+09:00"}]
    rid = add_sell_request("005930")
    manager.run(T_1000)
    assert status("sell_requests", "request_id", rid) == "NEW"
    assert fake.orders == []


def test_cooldown_error_from_api_keeps_new(fake):
    fake.fail["005930"] = AquaError(429, "SYMBOL_COOLDOWN", "tradable again at ...")
    did = add_decision("005930")
    manager.run(T_1000)
    assert status("decisions", "decision_id", did) == "NEW"


def test_trade_limit_stops_and_keeps_new(fake):
    fake.remaining = 1
    a, b = add_decision("A"), add_decision("B")
    manager.run(T_1000)
    assert status("decisions", "decision_id", a) == "DONE"
    assert status("decisions", "decision_id", b) == "NEW"
    assert len(fake.orders) == 1


def test_rejected_order_is_skipped(fake):
    fake.fail["005930"] = AquaError(422, "NO_LIQUIDITY")
    did = add_decision("005930")
    manager.run(T_1000)
    assert status("decisions", "decision_id", did) == "SKIPPED"


def test_orderbook_error_does_not_stop_others(fake):
    bad, good = add_decision("UNKNOWN"), add_decision("005930")
    manager.run(T_1000)
    assert status("decisions", "decision_id", bad) == "SKIPPED"
    assert status("decisions", "decision_id", good) == "DONE"


def test_timeout_but_accepted_is_recorded(fake):
    """응답은 못 받았지만 실제로 접수된 주문 → 주문 내역에서 찾아 DONE 처리(다시 주문하지 않음)"""
    fake.orders.append({"orderId": 99, "symbol": "005930", "order_code": "BUY", "quantity": 10,
                       "filledQuantity": 10, "status": "FILLED", "executedPrice": 10_000,
                       "createdAt": T_1000.isoformat(), "accepted": True})
    fake.fail["005930"] = requests.Timeout()
    did = add_decision("005930")
    manager.run(T_1000)
    assert status("decisions", "decision_id", did) == "DONE"
    assert positions() == {"005930"}


def test_timeout_not_found_is_skipped(fake):
    fake.fail["005930"] = requests.Timeout()
    did = add_decision("005930")
    manager.run(T_1000)
    assert status("decisions", "decision_id", did) == "SKIPPED"


def test_outside_market_hours_does_nothing(fake):
    did = add_decision("005930")
    manager.run(datetime(2026, 11, 2, 8, 0, tzinfo=KST))
    assert status("decisions", "decision_id", did) == "NEW"
    assert fake.orders == []


# ---------- 임시 규칙 · DRY_RUN ----------
def test_temp_rule_writes_top3_once(fake):
    for sym, rank in [("A", 1), ("B", 2), ("C", 3), ("D", 4)]:
        add_candidate(sym, rank)
    assert temp_rule.run(T_1000) == 3
    assert temp_rule.run(T_1000) == 0        # 같은 날 다시 돌려도 중복 기록 안 함
    with db.connect() as conn:
        syms = {r["symbol"] for r in conn.execute("SELECT symbol FROM decisions WHERE action = 'BUY'")}
    assert syms == {"A", "B", "C"}


def test_dry_run_never_calls_api(monkeypatch):
    monkeypatch.setattr(aqua_client, "DRY_RUN", True)
    monkeypatch.setattr(aqua_client, "_request", lambda *a, **k: pytest.fail("DRY_RUN 인데 API 호출됨"))
    resp = aqua_client.place_order("005930", "BUY", 3)
    assert resp["dryRun"] and resp["orderId"] < 0 and resp["filledQuantity"] == 3
    assert aqua_client.cancel_order(1)["dryRun"]
