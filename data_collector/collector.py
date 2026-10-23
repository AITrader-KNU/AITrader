"""4-1 데이터 수집기 — 담당: 데이터·인프라

쓰는 테이블: daily_candles, news
읽는 것: candidates (뉴스를 모을 종목), 포트폴리오 API (보유 종목, get_portfolio)
"""
from datetime import datetime, timedelta, timezone

from common import aqua_client, db
from common.config import get_config
from common.logger import get_logger

log = get_logger("data_collector")

# KST의 UTC 오프셋. 매매 기준값이 아닌 날짜 변환용 상수이다.
KST = timezone(timedelta(hours=9))


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Candle timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Candle timestamp must include a timezone")
    return parsed.astimezone(KST)


def _today():
    return datetime.now(KST).date()


def _year_ago(day):
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 윤년 2월 29일의 전년 기준은 2월 28일.
        return day.replace(year=day.year - 1, day=28)


def _required_history():
    cfg = get_config()
    candidates = cfg["candidates"]
    momentum = candidates["momentum"]
    return max(momentum["high_window_days"], candidates["ma_window"] + 1,
               momentum["lookback_days"] + momentum["skip_recent_days"] + 1,
               cfg["universe"]["liquidity_window"])


def _update_symbol(symbol, cutoff):
    with db.connect() as conn:
        latest, stored_count = conn.execute(
            "SELECT MAX(date),COUNT(*) FROM daily_candles WHERE symbol=? AND date<?",
            (symbol, cutoff.isoformat()),
        ).fetchone()
    required = _required_history()
    bootstrap = stored_count < required
    start = _year_ago(cutoff) if bootstrap else None
    rows = {}
    before = None
    while True:
        page = aqua_client.get_candles(symbol, before=before)
        if before is None and not page["candles"]:
            raise ValueError("No candles returned for latest page")
        reached_boundary = False
        for candle in page["candles"]:
            day = _timestamp(candle["timestamp"]).date()
            if day >= cutoff:
                continue
            if not bootstrap and latest is not None and day.isoformat() <= latest:
                reached_boundary = True
                continue
            if start is not None and day <= start:
                reached_boundary = True
            values = [candle[key] for key in
                      ("openPrice", "highPrice", "lowPrice", "closePrice", "volume")]
            if any(type(value) is not int for value in values):
                raise ValueError("OHLCV must contain integers")
            opening, high, low, close, volume = values
            if min(opening, high, low, close) <= 0 or volume < 0 or not low <= min(opening, close) <= max(opening, close) <= high:
                raise ValueError("Invalid OHLCV range")
            rows[day.isoformat()] = (symbol, day.isoformat(), *values)
        if reached_boundary and (not bootstrap or len(rows) >= required):
            break
        cursor = page.get("nextBefore")
        if not cursor:
            break
        cursor_time = _timestamp(cursor)
        if before is not None and cursor_time >= _timestamp(before):
            raise ValueError("Candle cursor did not move into the past")
        before = cursor
    if not rows and bootstrap:
        raise ValueError("No completed daily candles returned")
    if bootstrap:
        # 최근 달력 1년을 유지하되 전략 계산에 필요한 봉수가 부족하면 더 과거를 포함한다.
        rows = {day: row for index, (day, row) in enumerate(sorted(rows.items(), reverse=True))
                if index < required or day >= start.isoformat()}
        if len(rows) < required:
            log.warning("%s: only %d/%d candles available; strategy must handle short history",
                        symbol, len(rows), required)
    with db.connect() as conn:
        conn.executemany("""
            INSERT INTO daily_candles(symbol,date,open,high,low,close,volume)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(symbol,date) DO NOTHING
        """, rows.values())
    log.info("%s: saved %d completed candles", symbol, len(rows))


def update_daily_candles() -> None:
    """장 전: 유니버스 종목의 일봉을 daily_candles 에 갱신.
    - 최초에는 최근 1년과 전략 설정상 최소 봉수를 확보(get_candles 이어받기)
    - 이미 저장한 이력이 최소 봉수보다 짧으면 과거를 보충하고, 이후에는 최신 봉만 추가
    - 중단 기간이 길면 마지막 저장일까지 이어받고, 기존 이력은 재적재하지 않음
    - 오늘 및 미래 일봉 제외, 종목별 저장, 실패 시 후속 후보 선정 중단
    AQuA 조회는 common.aqua_client를 통해 실행한다."""
    cutoff = _today()
    stocks = aqua_client.list_stocks()
    if not stocks:
        raise ValueError("No stocks returned")
    db.init_db()
    failed = []
    for symbol in dict.fromkeys(stock["symbol"] for stock in stocks):
        try:
            _update_symbol(symbol, cutoff)
        except (aqua_client.AquaError, ValueError, KeyError, TypeError) as exc:
            failed.append(symbol)
            log.error("%s: collection failed (%s)", symbol, type(exc).__name__)
    if failed:
        raise RuntimeError(f"Daily candle collection failed: {', '.join(failed)}")


def collect_news() -> None:
    """후보·보유 종목의 최근 헤드라인을 news 에 저장.
    - 게시 시각(published_at) 반드시 저장, 같은 기사는 news_id 로 중복 제거
    - 종목명↔코드 매핑 규칙 적용('현대'·'삼성' 단독 매칭 금지 등)
    Google News RSS 사용. 조회 기간·타임아웃·별칭은 config.yaml의 news 설정을 따른다.
    설정이 없으면 후속 판단이 불완전한 입력으로 진행되지 않도록 예외를 낸다."""
    from data_collector.news import collect
    collect()
