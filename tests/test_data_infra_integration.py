"""Client and collector integration with fake HTTP and a temporary database."""
from datetime import datetime, timedelta, timezone

import requests

from common import aqua_client as api, db
from data_collector import collector, news


def test_client_to_candles_and_news(monkeypatch, tmp_path):
    now = datetime.fromisoformat("2026-10-23T09:30:00+09:00")
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "integration.db")
    monkeypatch.setattr(collector, "_today", lambda: now.date())
    monkeypatch.setattr(news, "now_kst", lambda: now)
    monkeypatch.setattr(api, "BASE_URL", "https://example.invalid")
    monkeypatch.setattr(api, "TEAM_CODE", "fixture-team")
    monkeypatch.setattr(api, "_token", None)
    monkeypatch.setattr(api, "_expires_at", None)
    monkeypatch.setattr(api, "_calls", __import__("collections").deque())
    candles = [{"timestamp": (now.date() - timedelta(days=i)).isoformat() + "T00:00:00+09:00",
                "openPrice": 100, "highPrice": 120, "lowPrice": 80,
                "closePrice": 100, "volume": 1000} for i in range(1, 401)]
    paths = []

    def request(method, url, **kwargs):
        path = url.removeprefix(api.BASE_URL)
        paths.append((method, path))
        if path == "/api/auth/login":
            assert method == "POST"
            body = {"accessToken": "fixture-token", "expiresAt":
                    (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()}
        else:
            assert method == "GET"  # No order or cancel request is allowed.
            assert kwargs["headers"]["Authorization"] == "Bearer fixture-token"
            if path == "/api/stocks":
                body = {"stocks": [{"symbol": "005930", "name": "삼성전자"}]}
            elif path == "/api/stocks/005930/candles":
                first = kwargs["params"].get("before") is None
                body = {"candles": candles[:200] if first else candles[200:],
                        "nextBefore": candles[200]["timestamp"] if first else None}
            elif path == "/api/portfolio":
                body = {"holdings": [{"symbol": "005930", "quantity": 1}]}
            else:
                raise AssertionError(path)
        response = requests.Response()
        response.status_code = 200
        response.json = lambda: body
        return response

    def rss(url, **kwargs):
        assert url == news.RSS_URL
        response = requests.Response()
        response.status_code = 200
        response._content = """<rss><channel><item><title>삼성전자 실적 발표</title>
          <pubDate>Fri, 23 Oct 2026 00:00:00 GMT</pubDate>
          <link>https://example.invalid/article</link><source>test</source>
          </item></channel></rss>""".encode()
        return response

    monkeypatch.setattr(api.requests, "request", request)
    monkeypatch.setattr(news.requests, "get", rss)
    collector.update_daily_candles()
    collector.update_daily_candles()
    collector.collect_news()
    collector.collect_news()
    assert paths.count(("POST", "/api/auth/login")) == 1
    assert paths.count(("GET", "/api/stocks/005930/candles")) == 3
    assert len(api._calls) == len(paths)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_candles").fetchone()[0] == 365
        assert conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 1
        assert conn.execute("SELECT published_at FROM news").fetchone()[0] == "2026-10-23T09:00:00+09:00"
        assert conn.execute("SELECT COUNT(*) FROM orders_log").fetchone()[0] == 0


def test_once_all_with_data_contract_fixtures(monkeypatch, tmp_path):
    """Exercise the scheduler CLI with temporary DB data; no external requests or orders."""
    import sys
    import pytest
    import main

    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "cli.db")
    monkeypatch.setattr(requests.sessions.Session, "request",
                        lambda *a, **k: pytest.fail("Unexpected network call"))
    monkeypatch.setattr(api, "place_order", lambda *a, **k: pytest.fail("Unexpected order"))
    monkeypatch.setattr(api, "cancel_order", lambda *a, **k: pytest.fail("Unexpected cancel"))
    events = []

    def candles_fixture():
        with db.connect() as conn:
            conn.execute("INSERT INTO daily_candles(symbol,date,close) VALUES ('005930','2026-10-22',100)")
        events.append("candles")

    def selector_fixture():
        with db.connect() as conn:
            assert conn.execute("SELECT close FROM daily_candles").fetchone()[0] == 100
            conn.execute("INSERT INTO candidates(run_date,symbol) VALUES ('2026-10-23','005930')")
        events.append("selector")

    def news_fixture():
        with db.connect() as conn:
            assert conn.execute("SELECT symbol FROM candidates").fetchone()[0] == "005930"
            conn.execute("""INSERT INTO news(news_id,symbol,title,published_at,collected_at)
                VALUES ('fixture','005930','fixture','2026-10-23T09:00:00+09:00','2026-10-23T09:30:00+09:00')""")
        events.append("news")

    def judge_fixture():
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM news WHERE published_at<=?",
                                ("2026-10-23T09:30:00+09:00",)).fetchone()[0] == 1
        events.append("judge")

    monkeypatch.setattr(main.collector, "update_daily_candles", candles_fixture)
    monkeypatch.setattr(main.selector, "run", selector_fixture)
    monkeypatch.setattr(main.collector, "collect_news", news_fixture)
    monkeypatch.setattr(main.judge, "run", judge_fixture)
    monkeypatch.setattr(main.temp_rule, "run", judge_fixture)   # 판단 자리: use_llm 이 false 면 임시 규칙이 불림
    monkeypatch.setattr(main.manager, "run", lambda: events.append("manager"))
    monkeypatch.setattr(main.monitor, "run", lambda: events.append("exit"))
    monkeypatch.setattr(sys, "argv", ["main.py", "--once", "all"])
    main.main()
    assert events == ["candles", "selector", "news", "judge", "manager", "exit", "manager"]


def test_news_failure_stops_judgment_and_orders(monkeypatch):
    import pytest
    import main

    def failure():
        raise RuntimeError("RSS failed")
    monkeypatch.setattr(main.collector, "collect_news", failure)
    monkeypatch.setattr(main.judge, "run", lambda: pytest.fail("Judgment after failed collection"))
    monkeypatch.setattr(main.manager, "run", lambda: pytest.fail("Orders after failed collection"))
    with pytest.raises(RuntimeError, match="RSS failed"):
        main.judge_job()
