import json
import sqlite3
from unittest.mock import patch
import pytest

from news_judge import judge

try:
    from common.db import STATUS_NEW
except ImportError:
    STATUS_NEW = "NEW"

@pytest.fixture
def test_db_setup(tmp_path, monkeypatch):
    db_file = tmp_path / "test_aqua.db"
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS candidates (
            run_date TEXT, symbol TEXT, name TEXT, momentum_rank INTEGER, ret_5d REAL, above_ma20 BOOLEAN
        );
        CREATE TABLE IF NOT EXISTS news (
            news_id TEXT PRIMARY KEY, symbol TEXT, title TEXT, published_at TEXT, source TEXT, url TEXT, collected_at TEXT
        );
        CREATE TABLE IF NOT EXISTS positions_meta (
            symbol TEXT PRIMARY KEY, bought_at TEXT, buy_price REAL, reason TEXT, decision_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, symbol TEXT, action TEXT, bad_news BOOLEAN, reason TEXT, risks TEXT, news_ids TEXT, status TEXT
        );
    """)
    conn.commit()
    conn.close()

    class TestDBConnection:
        def __enter__(self):
            self.conn = sqlite3.connect(str(db_file))
            return self.conn
        def __exit__(self, exc_type, exc_val, exc_tb):
            self.conn.commit()
            self.conn.close()

    monkeypatch.setattr(judge, "connect", lambda: TestDBConnection())
    monkeypatch.setattr(judge, "env", lambda key: "fake_gemini_api_key" if key == "GEMINI_API_KEY" else "")
    return str(db_file)

def test_judge_normal_buy_and_held_sell(test_db_setup):
    db_path = test_db_setup
    fixed_now_iso = "2026-10-24T09:30:00+09:00"
    fixed_date = "2026-10-24"

    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)", (fixed_date, "005930", "삼성전자", 1, 0.045, 1))
        c.execute("INSERT INTO positions_meta (symbol, bought_at, buy_price) VALUES (?, ?, ?)", ("000660", "2026-10-20T09:30:00+09:00", 180000))
        c.execute("INSERT INTO news (news_id, symbol, title, published_at) VALUES (?, ?, ?, ?)", ("n1", "005930", "삼성 수주 호재", "2026-10-24T08:50:00+09:00"))
        c.execute("INSERT INTO news (news_id, symbol, title, published_at) VALUES (?, ?, ?, ?)", ("n2", "000660", "하이닉스 공장 중단 악재", "2026-10-24T09:10:00+09:00"))

    fake_response = {
        "text": json.dumps([
            {"symbol": "005930", "action": "BUY", "bad_news": False, "reason": "호재", "risks": [], "news_ids": ["n1"]},
            {"symbol": "000660", "action": "SELL", "bad_news": True, "reason": "보유중 악재", "risks": [], "news_ids": ["n2"]}
        ]),
        "prompt_tokens": 1000, "candidates_tokens": 200, "total_tokens": 1200
    }

    with patch.object(judge, "get_current_kst_iso", return_value=fixed_now_iso), \
         patch.object(judge, "call_gemini_api", return_value=fake_response):
        judge.run()

    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("SELECT symbol, action, bad_news, status FROM decisions ORDER BY symbol")
        rows = c.fetchall()

    assert len(rows) == 2
    assert rows[0] == ("000660", "SELL", 1, STATUS_NEW)
    assert rows[1] == ("005930", "BUY", 0, STATUS_NEW)

def test_judge_future_news_exclusion(test_db_setup):
    db_path = test_db_setup
    fixed_now_iso = "2026-10-24T09:30:00+09:00"
    fixed_date = "2026-10-24"

    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)", (fixed_date, "035420", "NAVER", 1, 0.02, 1))
        c.execute("INSERT INTO news (news_id, symbol, title, published_at) VALUES (?, ?, ?, ?)", ("past_1", "035420", "과거 정상 뉴스", "2026-10-24T09:20:00+09:00"))
        c.execute("INSERT INTO news (news_id, symbol, title, published_at) VALUES (?, ?, ?, ?)", ("future_1", "035420", "미래 뉴스 유출 테스트", "2026-10-24T09:40:00+09:00"))

    captured = {}
    def fake_call(prompt, model, key):
        captured["text"] = prompt
        return {"text": json.dumps([{"symbol": "035420", "action": "WAIT", "bad_news": False, "reason": "중립", "risks": [], "news_ids": ["past_1"]}]),
                "prompt_tokens": 100, "candidates_tokens": 50, "total_tokens": 150}

    with patch.object(judge, "get_current_kst_iso", return_value=fixed_now_iso), \
         patch.object(judge, "call_gemini_api", side_effect=fake_call):
        judge.run()

    assert "과거 정상 뉴스" in captured["text"]
    assert "미래 뉴스 유출 테스트" not in captured["text"]

def test_judge_fallback_on_api_error(test_db_setup):
    db_path = test_db_setup
    fixed_now_iso = "2026-10-24T09:30:00+09:00"
    fixed_date = "2026-10-24"

    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)", (fixed_date, "005380", "현대차", 1, 0.01, 1))

    with patch.object(judge, "get_current_kst_iso", return_value=fixed_now_iso), \
         patch.object(judge, "call_gemini_api", side_effect=Exception("API 타임아웃")):
        judge.run()

    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("SELECT symbol, action, bad_news, status FROM decisions")
        row = c.fetchone()

    assert row is not None
    assert row[0] == "005380"
    assert row[1] == "WAIT"
    assert row[2] == 0
    assert row[3] == STATUS_NEW
