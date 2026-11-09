"""11월 추가 기능 연결 테스트: 시장 국면 상한·공시 차단(주문 관리자), 공시 프롬프트(판단 에이전트), main 순서.
실행: python -m pytest tests/test_november_wiring.py -v
"""
import copy
import json
import sqlite3
from unittest.mock import patch

import pytest

from common import db
from common.config import get_config
from news_judge import judge
from order_manager import manager
from tests.test_order_manager_run import FakeAqua, T_1000, TODAY, add_decision, held, status


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "test.db")
    db.init_db()
    f = FakeAqua(cash=75_000_000, holdings=[held("000001", 2500)])   # 주식 2,500만 + 현금 7,500만 = 총자산 1억
    monkeypatch.setattr(manager, "aqua", f)
    return f


def set_cfg(monkeypatch, **sections):
    cfg = copy.deepcopy(get_config())
    for name, values in sections.items():
        cfg[name] = {**cfg.get(name, {}), **values}
    monkeypatch.setattr(manager, "get_config", lambda: cfg)


def add_regime(regime, ratio, run_date=TODAY):
    with db.connect() as conn:
        conn.execute("INSERT INTO market_regime (run_date, regime, max_equity_ratio, created_at) VALUES (?,?,?,?)",
                     (run_date, regime, ratio, f"{run_date}T08:00:00+09:00"))


def add_disclosure(symbol, title, day=TODAY, no="20261102000001"):
    with db.connect() as conn:
        conn.execute("INSERT INTO disclosures (rcept_no, symbol, report_nm, rcept_dt, collected_at) VALUES (?,?,?,?,?)",
                     (no, symbol, title, day, f"{day}T09:00:00.000000+09:00"))


def bought(fake):
    return [(o["symbol"], o["quantity"]) for o in fake.orders if o["order_code"] == "BUY"]


# ---------- 시장 국면 상한 ----------
def test_down_regime_shrinks_budget(fake):
    add_regime("down", 0.35)                      # 상한 3,500만 − 주식 2,500만 = 1,000만 (정상 예산의 67%)
    add_decision("005930")
    manager.run(T_1000)
    assert bought(fake) == [("005930", 999)]      # 1,000만 ÷ (1 + 수수료) ÷ 1만원 → 999주


def test_equity_above_cap_skips(fake):
    add_regime("down", 0.2)                       # 상한 2,000만 < 주식 2,500만
    did = add_decision("005930")
    manager.run(T_1000)
    assert bought(fake) == [] and status("decisions", "decision_id", did) == "SKIPPED"


def test_missing_regime_uses_default(fake):
    add_decision("005930")                        # 기록 없음 → 횡보 60%: 여유 3,500만 > 종목당 1,500만
    manager.run(T_1000)
    assert bought(fake) == [("005930", 1499)]


def test_regime_disabled_means_no_cap(fake, monkeypatch):
    set_cfg(monkeypatch, regime={"enabled": False})
    add_regime("down", 0.2)
    add_decision("005930")
    manager.run(T_1000)
    assert bought(fake) == [("005930", 1499)]


def test_cap_counts_buys_in_same_run(fake):
    add_regime("sideways", 0.5)                   # 여유 2,500만: 첫 매수 1,500만 뒤 남는 약 1,000만
    manager_cands = [("005930", 1), ("000660", 2)]
    with db.connect() as conn:
        for sym, rank in manager_cands:
            conn.execute("INSERT INTO candidates (run_date, symbol, momentum_rank) VALUES (?,?,?)", (TODAY, sym, rank))
    add_decision("005930"); add_decision("000660")
    fake.place_order_orig = fake.place_order

    def place(symbol, side, quantity, price=None):
        resp = fake.place_order_orig(symbol, side, quantity, price)
        fake.cash -= resp["totalAmount"] + resp["fee"]
        resp["availableCash"] = fake.cash          # 실제 AQuA 처럼 남은 현금을 응답에 담음
        return resp
    fake.place_order = place
    manager.run(T_1000)
    assert [s for s, _ in bought(fake)] == ["005930", "000660"]
    assert 900 < bought(fake)[1][1] < 1499        # 두 번째는 정상(1,499주)이 아니라 남은 여유(약 1,000만)만큼만


# ---------- 공시 차단 ----------
def test_blocking_disclosure_skips_buy(fake):
    add_disclosure("005930", "유상증자결정")
    did = add_decision("005930")
    manager.run(T_1000)
    assert bought(fake) == [] and status("decisions", "decision_id", did) == "SKIPPED"


@pytest.mark.parametrize("title,day", [("단일판매ㆍ공급계약체결", TODAY), ("유상증자결정", "2026-10-20")])
def test_other_or_old_disclosure_does_not_block(fake, title, day):
    add_disclosure("005930", title, day=day)
    add_decision("005930")
    manager.run(T_1000)
    assert [s for s, _ in bought(fake)] == ["005930"]


def test_disclosure_disabled_does_not_block(fake, monkeypatch):
    set_cfg(monkeypatch, disclosure={"enabled": False})
    add_disclosure("005930", "횡령ㆍ배임혐의발생")
    add_decision("005930")
    manager.run(T_1000)
    assert [s for s, _ in bought(fake)] == ["005930"]


# ---------- 판단 에이전트 프롬프트 ----------
@pytest.fixture
def judge_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "judge.db")
    db.init_db()
    monkeypatch.setattr(judge, "env", lambda key: "fake" if key == "GEMINI_API_KEY" else "")
    with db.connect() as conn:
        conn.execute("INSERT INTO candidates (run_date, symbol, name, momentum_rank, ret_5d, above_ma20) "
                     "VALUES (?,?,?,?,?,?)", ("2026-11-09", "005930", "삼성전자", 1, 0.02, 1))
    return tmp_path


def run_judge(run_at="2026-11-09T09:30:00+09:00"):
    seen = {}

    def fake_call(prompt, model, key):
        seen["prompt"] = prompt
        return {"text": json.dumps([]), "prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}
    with patch.object(judge, "get_current_kst_iso", return_value=run_at), patch.object(judge, "call_gemini_api", fake_call):
        judge.run()
    return seen["prompt"]


def put(no, title, collected):
    with db.connect() as conn:
        conn.execute("INSERT INTO disclosures (rcept_no, symbol, report_nm, rcept_dt, collected_at) VALUES (?,?,?,?,?)",
                     (no, "005930", title, "2026-11-09", collected))


def test_prompt_lists_disclosures_known_at_run_time(judge_db):
    put("20261109000001", "유상증자결정", "2026-11-09T09:29:58.100000+09:00")
    put("20261109000002", "같은 초에 수집", "2026-11-09T09:30:00.900000+09:00")   # run_at 이 초 단위로 잘려 있어 포함
    put("20261109000003", "판단 뒤 수집", "2026-11-09T09:31:00.000000+09:00")
    prompt = run_judge()
    assert "[D:20261109000001] 유상증자결정" in prompt
    assert "같은 초에 수집" in prompt
    assert "판단 뒤 수집" not in prompt


def test_prompt_says_no_disclosures(judge_db):
    assert "(최근 공시 없음)" in run_judge()


def test_prompt_without_disclosure_setting(judge_db, monkeypatch):
    cfg = copy.deepcopy(get_config())
    cfg["disclosure"]["enabled"] = False
    monkeypatch.setattr(judge, "get_config", lambda: cfg)
    assert "최근 공시" not in run_judge()


def test_system_instruction_mentions_disclosure_ids():
    assert "D:접수번호" in judge.SYSTEM_INSTRUCTION


# ---------- main 순서 ----------
def test_premarket_runs_regime_after_selector_and_survives_failure(monkeypatch):
    import main
    order = []
    monkeypatch.setattr(main.collector, "update_daily_candles", lambda: order.append("candles"))
    monkeypatch.setattr(main.selector, "run", lambda: order.append("selector"))

    def boom():
        order.append("regime")
        raise RuntimeError("no data")
    monkeypatch.setattr(main.regime, "run", boom)
    main.premarket_job()                          # 예외가 밖으로 나오지 않음
    assert order == ["candles", "selector", "regime"]


def test_judge_continues_when_disclosure_fails(monkeypatch):
    import main
    order = []
    monkeypatch.setattr(main.collector, "collect_news", lambda: order.append("news"))

    def boom():
        raise RuntimeError("dart down")
    monkeypatch.setattr(main.disclosure, "run", boom)
    monkeypatch.setattr(main.judge, "run", lambda: order.append("judge"))
    monkeypatch.setattr(main.temp_rule, "run", lambda: order.append("judge"))
    monkeypatch.setattr(main.decision_prices, "run", lambda: order.append("prices"))
    monkeypatch.setattr(main.manager, "run", lambda: order.append("orders"))
    main.judge_job()
    assert order == ["news", "judge", "prices", "orders"]


# ---------- 자투리 매수 방지 (risk.min_buy_ratio) ----------
def test_small_room_under_cap_is_skipped(fake):
    add_regime("down", 0.252)                     # 여유 20만 원 < 정상 예산 1,500만 × 50%
    did = add_decision("005930")
    manager.run(T_1000)
    assert bought(fake) == [] and status("decisions", "decision_id", did) == "SKIPPED"


def test_low_cash_is_skipped(fake):
    fake.cash = 1_000_000                         # 현금 100만 원뿐 (상한과 무관하게 자투리)
    did = add_decision("005930")
    manager.run(T_1000)
    assert bought(fake) == [] and status("decisions", "decision_id", did) == "SKIPPED"


def test_half_position_boundary(fake):
    add_regime("down", 0.3)                       # 여유 500만 = 정상 예산의 33% < 50% → 안 삼
    did = add_decision("005930")
    manager.run(T_1000)
    assert bought(fake) == [] and status("decisions", "decision_id", did) == "SKIPPED"


def test_room_above_half_still_buys(fake):
    add_regime("down", 0.35)                      # 여유 1,000만 = 정상 예산의 67% ≥ 50% → 그 금액만큼 삼
    add_decision("005930")
    manager.run(T_1000)
    assert bought(fake) == [("005930", 999)]
