"""DART 공시 수집: run()은 공통 설정/DB를 사용하고 collect()는 의존성을 주입받는다.

import 시 I/O 없음. 판단·매수 차단·스케줄 연결은 각 담당 모듈의 책임이다.
"""
from __future__ import annotations

import io
import json
import math
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import urlopen
from xml.etree import ElementTree as ET

KST = timezone(timedelta(hours=9))
FATAL_CODES = {"010", "011", "012", "020", "800", "901"}


class CollectionError(Exception):
    """Only controlled messages: never include URLs, keys or raw response bodies."""

    def __init__(self, code):
        self.code = code if re.fullmatch(r"[A-Z_]+|[0-9]{3}", str(code)) else "INVALID_RESPONSE"
        super().__init__(self.code)


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    lookback_days: int = 5  # Calendar days, including today. Proposed convention.
    request_timeout_seconds: float = 10
    request_interval_seconds: float = 1
    corp_refresh_days: int = 7
    max_pages: int = 100  # Fail, never silently truncate, when this guard is exceeded.

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        for name in ("lookback_days", "corp_refresh_days", "max_pages"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("request_timeout_seconds", "request_interval_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid {name}")
        if self.request_timeout_seconds == 0:
            raise ValueError("timeout must be positive")


@dataclass
class Result:
    inserted: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stage_error: str | None = None
    skipped: bool = False


def as_kst(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise CollectionError("INVALID_TIME")
    return value.astimezone(KST)


def now_kst():
    return datetime.now(KST)


def iso(value):
    # Keep microseconds: rounding down could make an observation precede a judgment.
    return as_kst(value).isoformat(timespec="microseconds")


def valid_symbol(value):
    # Existing universe includes alphanumeric Korean stock codes.
    return isinstance(value, str) and re.fullmatch(r"[0-9A-Z]{6}", value) is not None


def checked_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise CollectionError("INVALID_DATE")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise CollectionError("INVALID_DATE") from None


def http_get(endpoint, params, timeout):
    """Transport may be replaced by a fixture. No retries; one request per call."""
    if endpoint not in {"list.json", "corpCode.xml"}:
        raise CollectionError("INVALID_ENDPOINT")
    try:
        url = "https://opendart.fss.or.kr/api/" + endpoint + "?" + urlencode(params)
        with urlopen(url, timeout=timeout) as response:
            return response.read()
    except Exception:
        raise CollectionError("HTTP_FAILED") from None


class DartClient:
    def __init__(self, api_key, settings, *, transport=http_get, sleep=time.sleep):
        if not isinstance(api_key, str) or not api_key.strip():
            raise CollectionError("MISSING_KEY")
        self._key = api_key.strip()
        self.settings = settings
        self.transport = transport
        self.sleep = sleep
        self._called = False

    def get(self, endpoint, **params):
        if self._called:
            self.sleep(self.settings.request_interval_seconds)
        self._called = True
        try:
            return self.transport(endpoint, {**params, "crtfc_key": self._key},
                                  self.settings.request_timeout_seconds)
        except CollectionError:
            raise
        except Exception:
            raise CollectionError("HTTP_FAILED") from None


def parse_corp_codes(content):
    try:
        if zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = [n for n in archive.namelist() if n.upper() == "CORPCODE.XML"]
                if len(names) != 1:
                    raise CollectionError("INVALID_CORP_ARCHIVE")
                root = ET.fromstring(archive.read(names[0]))
        else:
            root = ET.fromstring(content)  # DART errors can be plain XML.
        status = root.findtext("status")
        if status and status != "000":
            raise CollectionError(status if re.fullmatch(r"[0-9]{3}", status) else "INVALID_RESPONSE")
        rows = {}
        for item in root.findall("list"):
            symbol = (item.findtext("stock_code") or "").strip()
            if not symbol:  # Unlisted companies have no stock code.
                continue
            corp = (item.findtext("corp_code") or "").strip()
            name = (item.findtext("corp_name") or "").strip()
            if not valid_symbol(symbol) or not re.fullmatch(r"[0-9]{8}", corp) or not name:
                raise CollectionError("INVALID_CORP_ROW")
            if symbol in rows and rows[symbol] != (symbol, corp, name):
                raise CollectionError("AMBIGUOUS_CORP_CODE")
            rows[symbol] = (symbol, corp, name)
        if not rows:
            raise CollectionError("EMPTY_CORP_CODES")
        return sorted(rows.values())
    except CollectionError:
        raise
    except Exception:
        raise CollectionError("INVALID_CORP_ARCHIVE") from None


def refresh_corp_codes(conn, client, clock):
    rows = parse_corp_codes(client.get("corpCode.xml"))
    updated = iso(clock())  # Successful download/parse time, not request start.
    conn.execute("SAVEPOINT disclosure_mapping")
    try:
        # Replace a fully validated snapshot, removing obsolete mappings atomically.
        conn.execute("DELETE FROM corp_codes")
        conn.executemany("INSERT INTO corp_codes (symbol,corp_code,corp_name,updated_at) VALUES (?,?,?,?)",
                         [(*row, updated) for row in rows])
        conn.execute("RELEASE disclosure_mapping")
    except Exception:
        conn.execute("ROLLBACK TO disclosure_mapping")
        conn.execute("RELEASE disclosure_mapping")
        raise


def fetch_disclosures(client, symbol, corp_code, begin, end, clock):
    rows = {}
    expected_pages = None
    expected_count = None
    for page in range(1, client.settings.max_pages + 1):
        try:
            payload = json.loads(client.get(
                "list.json", corp_code=corp_code, bgn_de=begin.strftime("%Y%m%d"),
                end_de=end.strftime("%Y%m%d"), last_reprt_at="N", sort="date",
                sort_mth="asc", page_no=page, page_count=100))
            status = payload["status"]
            if status == "013" and page == 1:
                return []
            if status != "000":
                raise CollectionError(status if isinstance(status, str) and re.fullmatch(r"[0-9]{3}", status) else "INVALID_RESPONSE")
            total_pages = int(payload["total_page"])
            total_count = int(payload["total_count"])
            if int(payload["page_no"]) != page or total_pages < page or total_count <= 0:
                raise CollectionError("INVALID_PAGE")
            if total_pages > client.settings.max_pages:
                raise CollectionError("PAGE_LIMIT")
            if expected_pages is not None and (total_pages, total_count) != (expected_pages, expected_count):
                raise CollectionError("PAGES_CHANGED")
            expected_pages, expected_count = total_pages, total_count
            items = payload["list"]
            if not isinstance(items, list) or not items:
                raise CollectionError("EMPTY_PAGE")
            observed = iso(clock())
            for item in items:
                receipt, title = item["rcept_no"], item["report_nm"]
                day = checked_date(item["rcept_dt"])
                if (item["corp_code"] != corp_code or item.get("stock_code", "").strip() not in ("", symbol)
                        or not isinstance(receipt, str) or not re.fullmatch(r"[0-9]{14}", receipt)
                        or not isinstance(title, str) or not title.strip()
                        or not begin <= day <= end):
                    raise CollectionError("INVALID_DISCLOSURE")
                if receipt in rows:
                    raise CollectionError("DUPLICATE_PAGE_ROW")
                rows[receipt] = (receipt, symbol, title.strip(), day.isoformat(), observed)
            if page == total_pages:
                if len(rows) != total_count:
                    raise CollectionError("INCOMPLETE_PAGES")
                return list(rows.values())
        except CollectionError:
            raise
        except Exception:
            raise CollectionError("INVALID_RESPONSE") from None
    raise CollectionError("PAGE_LIMIT")


def store_disclosures(conn, rows):
    conn.execute("SAVEPOINT disclosure_rows")
    try:
        before = conn.total_changes
        for row in rows:
            existing = conn.execute("SELECT symbol FROM disclosures WHERE rcept_no=?", (row[0],)).fetchone()
            if existing and existing[0] != row[1]:
                raise CollectionError("RECEIPT_SYMBOL_CONFLICT")
            conn.execute("""INSERT INTO disclosures
                (rcept_no,symbol,report_nm,rcept_dt,collected_at) VALUES (?,?,?,?,?)
                ON CONFLICT(rcept_no) DO NOTHING""", row)
        inserted = conn.total_changes - before
        conn.execute("RELEASE disclosure_rows")
        return inserted
    except Exception:
        conn.execute("ROLLBACK TO disclosure_rows")
        conn.execute("RELEASE disclosure_rows")
        raise


def collect(conn, *, holdings, api_key, settings=None, transport=http_get,
            clock=now_kst, sleep=time.sleep):
    """Return controlled errors, so callers can continue news-based judgment.

    holdings: [{symbol, quantity}, ...], passed by the integration owner.
    No portfolio API calls, schema changes or automatic retry here.
    """
    cfg = settings or Settings()
    result = Result()
    if not cfg.enabled:
        result.skipped = True
        return result
    try:
        started = as_kst(clock())
        end = started.date()
        begin = end - timedelta(days=cfg.lookback_days - 1)
        symbols = {row[0] for row in conn.execute(
            "SELECT symbol FROM candidates WHERE run_date=?", (end.isoformat(),))}
        symbols.update(h["symbol"] for h in holdings if h["quantity"] > 0)
        if any(not valid_symbol(s) for s in symbols):
            raise CollectionError("INVALID_TARGET")
        if not symbols:
            result.skipped = True
            return result
        client = DartClient(api_key, cfg, transport=transport, sleep=sleep)
        timestamps = [as_kst(r[0]) for r in conn.execute("SELECT updated_at FROM corp_codes")]
        fresh = timestamps and all(t <= started < t + timedelta(days=cfg.corp_refresh_days) for t in timestamps)
        if not fresh:
            try:
                refresh_corp_codes(conn, client, clock)
            except CollectionError as exc:
                result.warnings.append("CORP_REFRESH_" + exc.code)
                if exc.code in FATAL_CODES or not timestamps:
                    raise
                result.warnings.append("USING_STALE_CORP_CODES")
        mapping = dict(conn.execute("SELECT symbol,corp_code FROM corp_codes"))
        ordered = sorted(symbols)
        for index, symbol in enumerate(ordered):
            if symbol not in mapping:
                result.failed[symbol] = "MISSING_CORP_CODE"
                continue
            try:
                rows = fetch_disclosures(client, symbol, mapping[symbol], begin, end, clock)
                result.inserted[symbol] = store_disclosures(conn, rows)
            except CollectionError as exc:
                result.failed[symbol] = exc.code
                if exc.code in FATAL_CODES:
                    result.stage_error = exc.code
                    for remaining in ordered[index + 1:]:
                        result.failed[remaining] = "NOT_ATTEMPTED"
                    break
            except sqlite3.Error:
                result.failed[symbol] = "DB_ERROR"
    except CollectionError as exc:
        result.stage_error = exc.code
    except sqlite3.Error:
        result.stage_error = "DB_ERROR"
    except Exception:
        result.stage_error = "INVALID_INPUT"
    return result


def run() -> Result:
    """Collect current candidates/holdings; failures return a result, never stop judge.

    Scheduler integration calls this after news collection. No automatic execution.
    """
    result = Result()
    log = None
    try:
        from common.config import get_config, env

        section = get_config().get("disclosure")
        if not isinstance(section, dict):
            return Result(stage_error="MISSING_SETTINGS")
        # Missing fields fail explicitly; NLP/order settings are not passed through.
        cfg = Settings(**{name: section[name] for name in Settings.__dataclass_fields__})
        if not cfg.enabled:
            return Result(skipped=True)
        from common.logger import get_logger

        log = get_logger("disclosure_collector")
        key = env("DART_API_KEY")
        if not key or not key.strip():
            result.stage_error = "MISSING_KEY"
        else:
            from common import aqua_client, db

            try:
                holdings = aqua_client.get_portfolio()["holdings"]
            except Exception:
                result.stage_error = "PORTFOLIO_FAILED"
            else:
                db.init_db()
                with db.connect() as conn:
                    result = collect(conn, holdings=holdings, api_key=key, settings=cfg,
                                     transport=http_get, clock=now_kst, sleep=time.sleep)
        log.info("DART inserted=%s failed=%s warnings=%s stage_error=%s skipped=%s",
                 result.inserted, result.failed, result.warnings, result.stage_error, result.skipped)
    except (KeyError, TypeError, ValueError):
        result.stage_error = "INVALID_SETTINGS"
    except sqlite3.Error:
        result.stage_error = "DB_ERROR"
    except Exception:
        # Never log exception text: transport errors can contain URLs with the key.
        result.stage_error = "COLLECTION_FAILED"
    if result.stage_error and log is not None:
        log.warning("DART collection incomplete: %s", result.stage_error)
    return result
