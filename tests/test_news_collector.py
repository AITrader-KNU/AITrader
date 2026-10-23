from datetime import datetime
from xml.sax.saxutils import escape

import pytest
import requests

from common import db
from data_collector import news

NOW = datetime.fromisoformat("2026-10-22T09:30:00+09:00")


def feed(title="삼성전자 실적 발표", published="Thu, 22 Oct 2026 00:00:00 GMT", url="https://example.com/article"):
    return (f"<rss><channel><item><title>{escape(title)}</title><pubDate>{published}</pubDate>"
            f"<link>{url}</link><source>테스트신문</source></item></channel></rss>").encode()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, news_settings):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "news.db")
    monkeypatch.setattr(news, "now_kst", lambda: NOW)
    monkeypatch.setattr(news.time, "sleep", lambda _: None)
    monkeypatch.setattr(news.aqua_client, "list_stocks", lambda: [
        {"symbol": "005930", "name": "삼성전자"}, {"symbol": "000660", "name": "SK하이닉스"}])
    db.init_db()


def test_time_html_and_repeat():
    rows, rejected = news.parse_feed(feed("<b>삼성전자</b> 실적 & 전망"), "005930", ["삼성전자"], NOW, 3)
    assert rejected == 0
    assert rows[0][2] == "삼성전자 실적 & 전망"
    assert rows[0][3] == "2026-10-22T09:00:00+09:00"
    assert rows[0][6] == "2026-10-22T09:30:00+09:00"
    assert news.store_rows(rows) == 1
    assert news.store_rows(rows) == 0


@pytest.mark.parametrize("published", ["invalid", "Thu, 22 Oct 2026 01:00:00 GMT", "Sun, 18 Oct 2026 00:00:00 GMT"])
def test_invalid_future_and_old_news(published):
    rows, rejected = news.parse_feed(feed(published=published), "005930", ["삼성전자"], NOW, 3)
    assert rows == []
    assert rejected == 1


def test_same_article_for_multiple_symbols():
    content = feed("삼성전자 SK하이닉스 반도체 소식")
    first, _ = news.parse_feed(content, "005930", ["삼성전자"], NOW, 3)
    second, _ = news.parse_feed(content, "000660", ["SK하이닉스"], NOW, 3)
    assert first[0][0] != second[0][0]
    assert news.store_rows(first + second) == 2


def test_names_and_aliases():
    assert not news.matches("삼성 그룹 뉴스", ["삼성전자", "삼성"])
    assert news.matches("네이버는 실적을 발표했다", ["NAVER", "네이버"])
    assert not news.matches("NAVERLAND", ["NAVER"])


def test_brokerage_does_not_match_car_company():
    assert not news.matches("현대차증권, 금융교육 진행", ["현대차"], ["현대차증권"])
    assert news.matches("현대차증권, 현대차 목표가 상향", ["현대차"], ["현대차증권"])


def test_platform_attribution_not_company_mention():
    assert not news.matches("교육청, 학생 지원 : 네이버 블로그", ["NAVER", "네이버"])
    assert news.matches("네이버 신규 서비스 발표 : 네이버 블로그", ["NAVER", "네이버"])


def test_equal_judgment_time_is_allowed():
    rows, _ = news.parse_feed(feed(published="Thu, 22 Oct 2026 00:30:00 GMT"), "005930", ["삼성전자"], NOW, 3)
    assert len(rows) == 1
    assert rows[0][3] == rows[0][6]


def test_auto_union_excludes_old_candidates_and_zero_holdings(monkeypatch):
    with db.connect() as conn:
        conn.execute("INSERT INTO candidates(run_date,symbol) VALUES ('2026-10-22','005930')")
        conn.execute("INSERT INTO candidates(run_date,symbol) VALUES ('2026-10-21','stale')")
    monkeypatch.setattr(news.aqua_client, "get_portfolio", lambda: {"holdings": [
        {"symbol": "005930", "quantity": 1}, {"symbol": "000660", "quantity": 2},
        {"symbol": "empty", "quantity": 0}]})
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = feed("삼성전자 SK하이닉스 반도체 소식")
        return response

    monkeypatch.setattr(news.requests, "get", get)
    assert news.collect() == {"005930": 1, "000660": 1}
    assert len(calls) == 2
    assert news.collect() == {"005930": 0, "000660": 0}


def test_rss_failure_not_treated_as_no_news(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout()
    monkeypatch.setattr(news.requests, "get", fail)
    with pytest.raises(RuntimeError, match="005930"):
        news.collect(["005930"])


def test_empty_rss_is_valid_but_html_is_not():
    assert news.parse_feed(b"<rss><channel/></rss>", "005930", ["삼성전자"], NOW, 3) == ([], 0)
    with pytest.raises(ValueError):
        news.parse_feed(b"<html/>", "005930", ["삼성전자"], NOW, 3)


def test_missing_team_settings_stops_before_api(monkeypatch):
    monkeypatch.setattr(news, "get_config", lambda: {})
    monkeypatch.setattr(news.aqua_client, "list_stocks", lambda: pytest.fail("Unexpected API call"))
    with pytest.raises(ValueError, match="team-reviewed"):
        news.collect(["005930"])


@pytest.mark.parametrize("key,value", [("lookback_days", 0), ("lookback_days", 1.5),
    ("request_timeout_seconds", 0), ("request_interval_seconds", -1),
    ("request_interval_seconds", float("nan"))])
def test_invalid_settings_stop_before_api(monkeypatch, key, value):
    cfg = news.get_config()
    cfg["news"][key] = value
    monkeypatch.setattr(news, "get_config", lambda: cfg)
    monkeypatch.setattr(news.aqua_client, "list_stocks", lambda: pytest.fail("Unexpected API call"))
    with pytest.raises(ValueError, match=key):
        news.collect(["005930"])


def test_empty_target_union_does_not_fetch_rss(monkeypatch):
    monkeypatch.setattr(news.aqua_client, "get_portfolio", lambda: {"holdings": []})
    monkeypatch.setattr(news.requests, "get", lambda *a, **k: pytest.fail("Unexpected RSS request"))
    assert news.collect() == {}


@pytest.fixture
def news_settings(monkeypatch):
    monkeypatch.setattr(news, "get_config", lambda: {"news": {
        "lookback_days": 3, "request_timeout_seconds": 20,
        "request_interval_seconds": 0, "aliases": {}, "match_exclusions": {},
    }})
