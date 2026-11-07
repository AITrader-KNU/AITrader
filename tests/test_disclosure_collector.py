import io
import json
import sqlite3
import unittest
import zipfile
from datetime import datetime, timedelta

from common.db import SCHEMA
from data_collector.disclosure import (KST, CollectionError, Settings, collect, iso,
                                       parse_corp_codes, store_disclosures)

NOW = datetime(2026, 11, 5, 9, 30, tzinfo=KST)
FAKE_KEY = "TEST_KEY_NOT_REAL"


def item(number=1, symbol="000001", corp="00000001", day="20261105", title="공시 제목"):
    return dict(rcept_no=f"20261105{number:06d}", stock_code=symbol,
                corp_code=corp, rcept_dt=day, report_nm=title)


def page(items, number=1, pages=1, count=None):
    return json.dumps(dict(status="000", page_no=number, total_page=pages,
                           total_count=len(items) if count is None else count,
                           list=items)).encode()


def archive(entries=None):
    entries = entries if entries is not None else [("000001", "00000001", "회사1"),
                                                   ("000002", "00000002", "회사2")]
    xml = "<result>" + "".join(
        f"<list><stock_code>{s}</stock_code><corp_code>{c}</corp_code>"
        f"<corp_name>{n}</corp_name></list>" for s, c, n in entries) + "</result>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("CORPCODE.xml", xml.encode())
    return buffer.getvalue()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO candidates(run_date,symbol) VALUES (?,?)", ("2026-11-05", "000001"))
        self.conn.executemany("INSERT INTO corp_codes VALUES (?,?,?,?)", [
            ("000001", "00000001", "회사1", iso(NOW)),
            ("000002", "00000002", "회사2", iso(NOW)),
        ])
        self.conn.commit()
        self.calls = []
        self.sleeps = []

    def run_collect(self, handler, **kwargs):
        def fake(endpoint, params, timeout):
            self.calls.append((endpoint, dict(params), timeout))
            return handler(endpoint, params)
        return collect(self.conn, holdings=kwargs.pop("holdings", []), api_key=FAKE_KEY,
                       transport=fake, clock=kwargs.pop("clock", lambda: NOW),
                       sleep=self.sleeps.append, **kwargs)

    def test_targets_union_positive_holdings_today_only(self):
        self.conn.execute("INSERT INTO candidates(run_date,symbol) VALUES ('2026-11-04','000003')")
        result = self.run_collect(lambda e, p: b'{"status":"013"}', holdings=[
            {"symbol": "000001", "quantity": 1}, {"symbol": "000002", "quantity": 2},
            {"symbol": "000004", "quantity": 0}, {"symbol": "000005", "quantity": -1}])
        self.assertEqual(result.inserted, {"000001": 0, "000002": 0})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [1])
        params = self.calls[0][1]
        self.assertEqual((params["bgn_de"], params["end_de"]), ("20261101", "20261105"))

    def test_all_pages_over_five_rows_and_first_timestamp_preserved(self):
        def handler(e, p):
            n = p["page_no"]
            return page([item(i) for i in (range(1, 5) if n == 1 else range(5, 8))], n, 2, 7)
        first = self.run_collect(handler)
        self.assertEqual(first.inserted, {"000001": 7})
        second = self.run_collect(handler, clock=lambda: NOW + timedelta(hours=1))
        self.assertEqual(second.inserted, {"000001": 0})
        self.assertEqual(self.conn.execute("SELECT DISTINCT collected_at FROM disclosures").fetchall(), [(iso(NOW),)])

    def test_observation_time_after_response_not_start_time(self):
        times = iter([NOW, NOW + timedelta(seconds=10, microseconds=123)])
        self.run_collect(lambda e, p: page([item()]), clock=lambda: next(times))
        timestamp = self.conn.execute("SELECT collected_at FROM disclosures").fetchone()[0]
        self.assertEqual(timestamp, iso(NOW + timedelta(seconds=10, microseconds=123)))
        self.assertGreater(timestamp, iso(NOW))

    def test_timeout_isolated_and_exception_does_not_leak_key(self):
        def handler(e, p):
            if p["corp_code"] == "00000001":
                raise TimeoutError("secret URL " + FAKE_KEY)
            return page([item(symbol="000002", corp="00000002")])
        result = self.run_collect(handler, holdings=[{"symbol": "000002", "quantity": 1}])
        self.assertEqual(result.failed, {"000001": "HTTP_FAILED"})
        self.assertEqual(result.inserted, {"000002": 1})
        self.assertNotIn(FAKE_KEY, repr(result))

    def test_second_page_failure_stores_no_partial_symbol(self):
        result = self.run_collect(lambda e, p: page([item()], 1, 2, 2)
                                  if p["page_no"] == 1 else b'{"status":"900"}')
        self.assertEqual(result.failed, {"000001": "900"})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM disclosures").fetchone()[0], 0)

    def test_fatal_status_stops_further_requests(self):
        for code in ["010", "012", "020", "800", "901"]:
            with self.subTest(code=code):
                self.calls.clear()
                result = self.run_collect(lambda e, p: json.dumps({"status": code}).encode(),
                                          holdings=[{"symbol": "000002", "quantity": 1}])
                self.assertEqual(result.stage_error, code)
                self.assertEqual(result.failed["000002"], "NOT_ATTEMPTED")
                self.assertEqual(len(self.calls), 1)

    def test_missing_mapping_is_explicit(self):
        result = self.run_collect(lambda e, p: b'{"status":"013"}',
                                  holdings=[{"symbol": "0120G0", "quantity": 1}])
        self.assertEqual(result.failed, {"0120G0": "MISSING_CORP_CODE"})

    def test_disabled_and_empty_targets_do_not_request(self):
        result = self.run_collect(lambda e, p: self.fail("unexpected request"), settings=Settings(enabled=False))
        self.assertTrue(result.skipped)
        self.conn.execute("DELETE FROM candidates")
        result = self.run_collect(lambda e, p: self.fail("unexpected request"))
        self.assertTrue(result.skipped)

    def test_fresh_mapping_does_not_download(self):
        self.run_collect(lambda e, p: b'{"status":"013"}')
        self.assertEqual([c[0] for c in self.calls], ["list.json"])

    def test_weekly_refresh_replaces_complete_snapshot(self):
        self.conn.execute("UPDATE corp_codes SET updated_at=?", (iso(NOW - timedelta(days=7)),))
        def handler(e, p):
            return archive([("000001", "00000003", "새회사")]) if e == "corpCode.xml" else b'{"status":"013"}'
        self.run_collect(handler)
        self.assertEqual([c[0] for c in self.calls], ["corpCode.xml", "list.json"])
        self.assertEqual(self.calls[1][1]["corp_code"], "00000003")
        self.assertEqual(self.conn.execute("SELECT symbol FROM corp_codes").fetchall(), [("000001",)])

    def test_failed_refresh_preserves_old_mapping_and_warns(self):
        self.conn.execute("UPDATE corp_codes SET updated_at=?", (iso(NOW - timedelta(days=8)),))
        result = self.run_collect(lambda e, p: b"broken zip" if e == "corpCode.xml" else b'{"status":"013"}')
        self.assertIn("USING_STALE_CORP_CODES", result.warnings)
        self.assertEqual(result.inserted, {"000001": 0})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM corp_codes").fetchone()[0], 2)

    def test_initial_refresh_failure_has_no_list_requests(self):
        self.conn.execute("DELETE FROM corp_codes")
        result = self.run_collect(lambda e, p: b'<result><status>010</status></result>')
        self.assertEqual(result.stage_error, "010")
        self.assertEqual(len(self.calls), 1)

    def test_bad_rows_and_pagination_are_not_success(self):
        bad_item = item(corp="99999999")
        for payload, expected in [
            (page([bad_item]), "INVALID_DISCLOSURE"),
            (page([item(day="20261031")]), "INVALID_DISCLOSURE"),
            (page([item(day="20261106")]), "INVALID_DISCLOSURE"),
            (page([item(day="20261301")]), "INVALID_DATE"),
            (page([item()], count=2), "INCOMPLETE_PAGES"),
            (page([item(), item()]), "DUPLICATE_PAGE_ROW"),
            (page([item()], pages=101), "PAGE_LIMIT"),
            (b"not json", "INVALID_RESPONSE"),
            (json.dumps({"status": FAKE_KEY}).encode(), "INVALID_RESPONSE"),
        ]:
            with self.subTest(expected=expected):
                result = self.run_collect(lambda e, p: payload)
                self.assertEqual(result.failed, {"000001": expected})
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM disclosures").fetchone()[0], 0)

    def test_changed_page_totals_fail(self):
        result = self.run_collect(lambda e, p: page([item(p["page_no"])], p["page_no"], 2,
                                                   2 if p["page_no"] == 1 else 3))
        self.assertEqual(result.failed["000001"], "PAGES_CHANGED")

    def test_mapping_parser_alphanumeric_unlisted_conflict(self):
        self.assertEqual(parse_corp_codes(archive([("0120G0", "00000001", "회사"),
                                                   ("", "00000002", "비상장")])),
                         [("0120G0", "00000001", "회사")])
        with self.assertRaises(CollectionError):
            parse_corp_codes(archive([("000001", "00000001", "A"), ("000001", "00000002", "B")]))

    def test_store_rollback_preserves_existing_and_caller_transaction(self):
        existing = ("20261105000001", "000001", "원본", "2026-11-05", iso(NOW))
        store_disclosures(self.conn, [existing])
        self.conn.execute("INSERT INTO candidates(run_date,symbol) VALUES ('2026-11-05','000003')")
        with self.assertRaises(CollectionError):
            store_disclosures(self.conn, [("20261105000002", *existing[1:]),
                                           (existing[0], "000002", *existing[2:])])
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT * FROM disclosures").fetchall(), [existing])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 2)

    def test_settings_validation(self):
        for kwargs in ({"lookback_days": 0}, {"max_pages": True}, {"request_timeout_seconds": 0},
                       {"request_interval_seconds": float("nan")}, {"enabled": "false"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Settings(**kwargs)


if __name__ == "__main__":
    unittest.main()
