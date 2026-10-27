"""금요일 청산 점검(tools/exit_review.py)이 조건을 맞게 판정하는지 확인."""
from common import db
from common.config import get_config
from tools.exit_review import review

CFG = get_config()


def setup(tmp_path, monkeypatch, trades):
    """trades: [(symbol, why, buy, sell, sell_date, [매도 후 종가...])]"""
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "t.db")
    db.init_db()
    with db.connect() as conn:
        for i, (sym, why, buy, sell, day, closes) in enumerate(trades, 1):
            conn.execute("INSERT INTO orders_log (order_id, symbol, side, quantity, executed_price, created_at) "
                         "VALUES (?,?,?,?,?,?)", (i * 10, sym, "BUY", 1, buy, f"{day}T09:30:00+09:00"))
            rid = conn.execute("INSERT INTO sell_requests (created_at, symbol, why, status) VALUES (?,?,?,'DONE')",
                               (f"{day}T10:00:00+09:00", sym, why)).lastrowid
            conn.execute("INSERT INTO orders_log (order_id, symbol, side, quantity, executed_price, source, created_at) "
                         "VALUES (?,?,?,?,?,?,?)", (i * 10 + 1, sym, "SELL", 1, sell, f"sell_request:{rid}",
                                                    f"{day}T10:00:01+09:00"))
            for k, c in enumerate(closes, 1):
                conn.execute("INSERT INTO daily_candles (symbol, date, close) VALUES (?,?,?)",
                             (sym, f"2026-11-{10 + k:02d}", c))
    with db.connect() as conn:
        return review(conn, None, CFG)


def stop(sym, closes):
    return (sym, "STOP_LOSS", 10000, 9600, "2026-11-10", closes)


def test_recovery_condition_met(tmp_path, monkeypatch):
    up, down = [9700, 9800, 9900, 10100, 10200], [9500, 9400, 9300, 9200, 9100]
    r = setup(tmp_path, monkeypatch, [stop("A", up), stop("B", up), stop("C", up), stop("D", down), stop("E", down)])
    assert r["recovery"].startswith("조정 조건 충족") and "3/5" in r["recovery"]
    assert r["fill"].startswith("미충족")                      # 평균 -4%, 기준 -5%


def test_too_few_cases_is_on_hold(tmp_path, monkeypatch):
    r = setup(tmp_path, monkeypatch, [stop("A", [10100] * 5)])
    assert r["recovery"].startswith("판단 보류") and r["take_extra"].startswith("판단 보류")


def test_not_ready_until_window_passes(tmp_path, monkeypatch):
    # 매도 후 종가가 2일치뿐이면 회복 판단에서 빠짐 → 5건이어도 판단 대상 0건
    r = setup(tmp_path, monkeypatch, [stop(s, [10100, 10200]) for s in "ABCDE"])
    assert r["recovery"].startswith("판단 보류") and all(not e["ready"] for e in r["exits"])


def test_bad_fill_condition(tmp_path, monkeypatch):
    r = setup(tmp_path, monkeypatch, [(s, "STOP_LOSS", 10000, 9400, "2026-11-10", [9000] * 5) for s in "ABCDE"])
    assert r["fill"].startswith("조정 조건 충족")             # 평균 -6% < -5%
