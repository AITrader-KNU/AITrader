"""공통 설정/DB 연결 검증. 실제 HTTP·포트폴리오·키를 사용하지 않는다."""
from dataclasses import asdict
import logging

import pytest

from common import aqua_client, config, db, logger
from data_collector import disclosure
from tests.test_disclosure_collector import NOW, archive, item, page


@pytest.fixture
def setup_run(tmp_path, monkeypatch):
    section = asdict(disclosure.Settings())
    monkeypatch.setattr(config, "get_config", lambda: {"disclosure": section})
    monkeypatch.setattr(config, "env", lambda name: "fixture-key")
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "disclosure.db")
    monkeypatch.setattr(logger, "get_logger", lambda name: logging.getLogger("fixture.disclosure"))
    monkeypatch.setattr(aqua_client, "get_portfolio", lambda: {"holdings": [{"symbol": "000001", "quantity": 1}]})
    monkeypatch.setattr(disclosure, "now_kst", lambda: NOW)
    monkeypatch.setattr(disclosure.time, "sleep", lambda _: None)
    monkeypatch.setattr(disclosure, "http_get", lambda *a, **k: pytest.fail("Unexpected HTTP request"))
    return section


def test_run_downloads_mapping_stores_and_repeats(setup_run, monkeypatch):
    calls = []

    def transport(endpoint, params, timeout):
        calls.append(endpoint)
        return archive() if endpoint == "corpCode.xml" else page([item()])

    monkeypatch.setattr(disclosure, "http_get", transport)
    assert disclosure.run().inserted == {"000001": 1}
    assert disclosure.run().inserted == {"000001": 0}
    assert calls == ["corpCode.xml", "list.json", "list.json"]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM disclosures").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM orders_log").fetchone()[0] == 0


def test_disabled_has_no_portfolio_or_db_io(setup_run, monkeypatch):
    setup_run["enabled"] = False
    monkeypatch.setattr(aqua_client, "get_portfolio", lambda: pytest.fail("Unexpected portfolio"))
    monkeypatch.setattr(db, "init_db", lambda: pytest.fail("Unexpected DB init"))
    assert disclosure.run().skipped


def test_missing_key_does_not_query_portfolio(setup_run, monkeypatch):
    monkeypatch.setattr(config, "env", lambda name: None)
    monkeypatch.setattr(aqua_client, "get_portfolio", lambda: pytest.fail("Unexpected portfolio"))
    assert disclosure.run().stage_error == "MISSING_KEY"


def test_portfolio_failure_is_sanitized(setup_run, monkeypatch, caplog):
    def fail():
        raise RuntimeError("secret-fixture-url")
    monkeypatch.setattr(aqua_client, "get_portfolio", fail)
    assert disclosure.run().stage_error == "PORTFOLIO_FAILED"
    assert "secret-fixture-url" not in caplog.text


def test_bad_settings_and_foreign_fields(setup_run, monkeypatch):
    del setup_run["lookback_days"]
    assert disclosure.run().stage_error == "INVALID_SETTINGS"
    setup_run["lookback_days"] = 5
    setup_run["max_per_stock"] = 5  # NLP settings must not break the collector.
    setup_run["enabled"] = False
    assert disclosure.run().skipped
    monkeypatch.setattr(config, "get_config", lambda: {})
    assert disclosure.run().stage_error == "MISSING_SETTINGS"


def test_db_failure_returns_code(setup_run, monkeypatch):
    import sqlite3
    def fail():
        raise sqlite3.OperationalError("fixture failure")
    monkeypatch.setattr(db, "init_db", fail)
    assert disclosure.run().stage_error == "DB_ERROR"


def test_schema_upgrade_preserves_existing_rows(setup_run):
    db.init_db()
    with db.connect() as conn:
        conn.execute("INSERT INTO news VALUES ('n','000001','existing','t','source','url','t')")
        conn.execute("INSERT INTO disclosures VALUES ('r','000001','existing','2026-11-05','t')")
    db.init_db()
    with db.connect() as conn:
        assert conn.execute("SELECT title FROM news").fetchone()[0] == "existing"
        assert conn.execute("SELECT report_nm FROM disclosures").fetchone()[0] == "existing"
