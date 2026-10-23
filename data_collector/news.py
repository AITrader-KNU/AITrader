"""Google News RSS 헤드라인 수집. 기사 본문과 LLM 판단은 처리하지 않는다."""
import hashlib
import html
import math
import re
import time
import unicodedata
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests

from common import aqua_client, db
from common.config import get_config
from common.logger import get_logger
from data_collector.collector import KST


def as_kst(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("News timestamp must include a timezone")
    return value.astimezone(KST)


def iso_kst(value):
    return as_kst(value).isoformat(timespec="seconds")


def now_kst():
    return datetime.now(KST)


log = get_logger("news_collector")
RSS_URL = "https://news.google.com/rss/search"


def clean(value):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", html.unescape(value or ""))).split())


def matches(title, names, excluded_names=()):
    # 플랫폼 출처 표시는 기업 언급으로 취급하지 않는다.
    title = re.sub(r"\s*[:|]\s*네이버\s*블로그\s*$", "", title)
    normalized = unicodedata.normalize("NFKC", title).casefold()
    for excluded in excluded_names:
        normalized = normalized.replace(unicodedata.normalize("NFKC", excluded).casefold(), " ")
    for name in names:
        # 그룹명 단독으로 자회사까지 연결하지 않는다.
        if name in {"삼성", "현대"} or len(name.strip()) < 2:
            continue
        pattern = re.escape(unicodedata.normalize("NFKC", name).casefold())
        if re.search(r"(?<![a-z0-9가-힣])" + pattern + r"(?![a-z0-9])", normalized):
            return True
    return False


def parse_feed(content, symbol, names, collected_at, days, excluded_names=()):
    root = ET.fromstring(content)
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise ValueError("Expected RSS channel")
    rows = {}
    rejected = 0
    for item in channel.findall("item"):
        try:
            title = clean(item.findtext("title"))
            source = clean(item.findtext("source"))
            # 매체 이름이 종목명인 경우 기사 제목 매칭에 사용하지 않는다.
            headline = title.removesuffix(" - " + source) if source else title
            url = (item.findtext("link") or "").strip()
            if not headline or not urlsplit(url).netloc or urlsplit(url).scheme not in {"https", "http"}:
                raise ValueError("Invalid headline or URL")
            published = as_kst(parsedate_to_datetime(item.findtext("pubDate") or ""))
            if not collected_at - timedelta(days=days) <= published <= collected_at:
                rejected += 1
                continue
            if not matches(headline, names, excluded_names):
                rejected += 1
                continue
            # 한 기사가 여러 종목에 관련될 수 있으므로 종목별 링크를 보존한다.
            news_id = hashlib.sha256(f"{symbol}\n{url}".encode()).hexdigest()
            rows[news_id] = (news_id, symbol, headline, iso_kst(published), source,
                             url, iso_kst(collected_at))
        except (ValueError, TypeError, OverflowError):
            rejected += 1
    return list(rows.values()), rejected


def store_rows(rows):
    with db.connect() as conn:
        before = conn.total_changes
        conn.executemany("""INSERT INTO news
            (news_id,symbol,title,published_at,source,url,collected_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(news_id) DO NOTHING""", rows)
        return conn.total_changes - before


def collect(symbols=None):
    cfg = get_config().get("news")
    if cfg is None:
        raise ValueError("News collection requires team-reviewed news settings in config.yaml; "
                         "ask the team lead to apply the reviewed news configuration")
    days = cfg.get("lookback_days")
    if type(days) is not int or days <= 0:
        raise ValueError("news.lookback_days must be a positive integer")
    for key in ("request_timeout_seconds", "request_interval_seconds"):
        value = cfg.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"news.{key} must be a finite non-negative number")
    if cfg["request_timeout_seconds"] == 0:
        raise ValueError("news.request_timeout_seconds must be positive")
    db.init_db()
    started = now_kst()
    if symbols is None:
        with db.connect() as conn:
            symbols = [row[0] for row in conn.execute(
                "SELECT symbol FROM candidates WHERE run_date=?", (started.date().isoformat(),))]
        symbols += [holding["symbol"] for holding in aqua_client.get_portfolio()["holdings"]
                    if holding["quantity"] > 0]
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        log.info("No candidate or held symbols; news collection skipped")
        return {}
    stock_names = {stock["symbol"]: stock["name"] for stock in aqua_client.list_stocks()}
    unknown = set(symbols) - stock_names.keys()
    if unknown:
        raise ValueError(f"Unknown symbols: {', '.join(sorted(unknown))}")
    results = {}
    failed = []
    for index, symbol in enumerate(symbols):
        if index:
            time.sleep(cfg["request_interval_seconds"])
        names = list(dict.fromkeys([stock_names[symbol], *cfg.get("aliases", {}).get(symbol, [])]))
        query = "(" + " OR ".join('"' + name + '"' for name in names) + f") when:{days}d"
        try:
            response = requests.get(RSS_URL, params={"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
                                    timeout=cfg["request_timeout_seconds"],
                                    headers={"User-Agent": "AITrader-NewsCollector/1.0"})
            response.raise_for_status()
            rows, rejected = parse_feed(response.content, symbol, names, started, days,
                                        cfg.get("match_exclusions", {}).get(symbol, []))
            results[symbol] = store_rows(rows)
            log.info("%s: news accepted=%d inserted=%d rejected=%d", symbol, len(rows), results[symbol], rejected)
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            failed.append(symbol)
            log.error("%s: RSS collection failed (%s)", symbol, type(exc).__name__)
    if failed:
        raise RuntimeError(f"News collection failed: {', '.join(failed)}")
    return results
