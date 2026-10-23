from datetime import date, timedelta
import inspect

import pytest

from common import db
from data_collector import collector


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "candles.db")
    monkeypatch.setattr(collector, "_today", lambda: date(2026, 10, 23))
    monkeypatch.setattr(collector.aqua_client, "list_stocks", lambda: [{"symbol": "005930"}])
    db.init_db()


def candle(day, close=100):
    return {"timestamp": f"{day}T00:00:00+09:00", "openPrice": 100,
            "highPrice": 120, "lowPrice": 80, "closePrice": close, "volume": 1000}


def rows():
    with db.connect() as conn:
        return [tuple(row) for row in conn.execute("SELECT * FROM daily_candles ORDER BY symbol,date")]


def test_pagination_completed_days_and_repeat(monkeypatch):
    items = [candle(date(2026, 10, 23) - timedelta(days=i)) for i in range(401)]
    calls = []
    def fetch(symbol, before=None):
        calls.append(before)
        return {"candles": items[:200] if before is None else items[200:],
                "nextBefore": items[200]["timestamp"] if before is None else None}
    monkeypatch.setattr(collector.aqua_client, "get_candles", fetch)
    assert collector.update_daily_candles() is None
    first = rows()
    assert len(first) == 365
    assert first[0][1] == "2025-10-23"
    assert first[-1][1] == "2026-10-22"
    collector.update_daily_candles()
    assert rows() == first
    assert len(calls) == 3  # 최초 2페이지, 재실행은 최신 1페이지만.


def test_incremental_only_adds_after_latest_date(monkeypatch):
    monkeypatch.setattr(collector, "_required_history", lambda: 1)
    with db.connect() as conn:
        conn.execute("INSERT INTO daily_candles VALUES ('005930','2026-10-20',100,120,80,90,1000)")
    monkeypatch.setattr(collector.aqua_client, "get_candles", lambda *a, **k: {
        "candles": [candle(f"2026-10-{d}") for d in (20, 21, 22)]})
    collector.update_daily_candles()
    assert len(rows()) == 3
    assert rows()[0][5] == 90  # 기존 행을 덮어쓰지 않는다.
    assert all(row[5] == 100 for row in rows()[1:])


def test_long_outage_pages_back_to_latest_date(monkeypatch):
    monkeypatch.setattr(collector, "_required_history", lambda: 1)
    with db.connect() as conn:
        conn.execute("INSERT INTO daily_candles VALUES ('005930','2026-10-19',100,120,80,100,1000)")
    calls = []
    def fetch(symbol, before=None):
        calls.append(before)
        days = (22, 21) if before is None else (20, 19)
        return {"candles": [candle(f"2026-10-{d}") for d in days],
                "nextBefore": "2026-10-20T00:00:00+09:00" if before is None else None}
    monkeypatch.setattr(collector.aqua_client, "get_candles", fetch)
    collector.update_daily_candles()
    assert len(calls) == 2
    assert len(rows()) == 4


def test_leap_year_boundary():
    assert collector._year_ago(date(2024, 2, 29)) == date(2023, 2, 28)


def test_extend_existing_short_year_to_required_history(monkeypatch):
    # A year can contain fewer trading sessions than the strategy requires.
    items = [candle(date(2026, 10, 23) - timedelta(days=2 * i)) for i in range(1, 401)]
    with db.connect() as conn:
        conn.executemany("INSERT INTO daily_candles VALUES (?,?,?,?,?,?,?)",
            [("005930", item["timestamp"][:10], 100, 120, 80, 100, 1000) for item in items[:244]])
    calls = []
    def fetch(symbol, before=None):
        calls.append(before)
        return {"candles": items[:200] if before is None else items[200:],
                "nextBefore": items[200]["timestamp"] if before is None else None}
    monkeypatch.setattr(collector.aqua_client, "get_candles", fetch)
    collector.update_daily_candles()
    assert len(rows()) == collector._required_history() == 250
    assert len(calls) == 2
    first = rows()
    collector.update_daily_candles()
    assert rows() == first
    assert len(calls) == 3


def test_available_short_history_is_kept_without_fabrication(monkeypatch, caplog):
    monkeypatch.setattr(collector.aqua_client, "get_candles", lambda *a, **k: {
        "candles": [candle("2026-10-22"), candle("2026-10-21")], "nextBefore": None})
    collector.update_daily_candles()
    assert len(rows()) == 2
    assert "2/250" in caplog.text


def test_failure_discards_partial_symbol_and_continues(monkeypatch):
    monkeypatch.setattr(collector.aqua_client, "list_stocks", lambda: [{"symbol": "bad"}, {"symbol": "good"}])
    def fetch(symbol, before=None):
        if symbol == "bad" and before:
            raise collector.aqua_client.AquaError(502, "MARKET_DATA_UNAVAILABLE")
        return {"candles": [candle("2026-10-22")],
                "nextBefore": "2026-10-21T00:00:00+09:00" if symbol == "bad" else None}
    monkeypatch.setattr(collector.aqua_client, "get_candles", fetch)
    with pytest.raises(RuntimeError, match="bad"):
        collector.update_daily_candles()
    assert [row[0] for row in rows()] == ["good"]


@pytest.mark.parametrize("field,value", [("volume", -1), ("volume", True), ("closePrice", 0),
                                          ("highPrice", 50), ("timestamp", None),
                                          ("timestamp", "2026-10-22T00:00:00")])
def test_invalid_candle_does_not_write(monkeypatch, field, value):
    item = candle("2026-10-22")
    item[field] = value
    monkeypatch.setattr(collector.aqua_client, "get_candles", lambda *a, **k: {"candles": [item]})
    with pytest.raises(RuntimeError):
        collector.update_daily_candles()
    assert rows() == []


def test_cursor_must_progress(monkeypatch):
    monkeypatch.setattr(collector.aqua_client, "get_candles", lambda *a, **k: {
        "candles": [candle("2026-10-22")], "nextBefore": "2026-10-21T00:00:00+09:00"})
    with pytest.raises(RuntimeError):
        collector.update_daily_candles()
    assert rows() == []


def test_kst_boundary():
    assert collector._timestamp("2026-10-22T15:00:00Z").date() == date(2026, 10, 23)


def test_empty_latest_page_is_not_successful_incremental_update(monkeypatch):
    monkeypatch.setattr(collector, "_required_history", lambda: 1)
    with db.connect() as conn:
        conn.execute("INSERT INTO daily_candles VALUES ('005930','2026-10-20',100,120,80,100,1000)")
    original = rows()
    monkeypatch.setattr(collector.aqua_client, "get_candles", lambda *a, **k: {
        "candles": [], "nextBefore": None})
    with pytest.raises(RuntimeError, match="005930"):
        collector.update_daily_candles()
    assert rows() == original


def test_bad_timestamp_does_not_stop_other_symbols(monkeypatch):
    monkeypatch.setattr(collector.aqua_client, "list_stocks", lambda: [
        {"symbol": "bad"}, {"symbol": "good"}])

    def fetch(symbol, before=None):
        item = candle("2026-10-22")
        if symbol == "bad":
            item["timestamp"] = None
        return {"candles": [item], "nextBefore": None}

    monkeypatch.setattr(collector.aqua_client, "get_candles", fetch)
    with pytest.raises(RuntimeError, match="bad"):
        collector.update_daily_candles()
    assert [row[0] for row in rows()] == ["good"]


def test_original_entrypoint_signatures():
    for entry in (collector.update_daily_candles, collector.collect_news):
        assert not inspect.signature(entry).parameters
        assert inspect.signature(entry).return_annotation is None
