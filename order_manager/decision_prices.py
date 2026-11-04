"""판단 시점 가격 기록 — 규칙 vs LLM 비교 분석용 (매매 판단에는 쓰지 않음)

main.judge_job 에서 판단 에이전트(또는 임시 규칙)가 decisions 를 쓴 직후, 주문 관리자보다 먼저 실행한다.
오늘 판단 중 아직 가격이 없는 행의 현재가를 한 번에 조회해 decision_prices 에 남긴다.
사지 않은 종목(WAIT)도 남겨서, 나중에 '샀다면' 수익률을 같은 기준(판단 직후 가격)으로 계산할 수 있게 한다.

읽는 곳: decisions, AQuA get_prices(현재가 일괄 조회, docs/API.md)
쓰는 곳: decision_prices (decision_id 당 한 행. 이미 있으면 다시 쓰지 않음)

실패해도 매매에 영향이 없도록 예외는 로그만 남기고 0 을 돌려준다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from common import aqua_client
from common.config import get_config
from common.db import connect
from common.logger import get_logger

log = get_logger("decision_prices")

MAX_SYMBOLS_PER_CALL = 200   # docs/API.md: 현재가 일괄 조회는 한 번에 최대 200개


def run(now: datetime | None = None) -> int:
    """기록한 행 수를 돌려준다. 어떤 오류가 나도 예외를 내지 않는다(다음 단계인 주문을 막지 않게)."""
    try:
        return _record(now or datetime.now(ZoneInfo(get_config()["timezone"])))
    except Exception:
        log.exception("판단 시점 가격 기록 실패 — 매매는 그대로 진행")
        return 0


def _record(now: datetime) -> int:
    today = now.date().isoformat()
    with connect() as conn:
        rows = conn.execute(
            """SELECT d.decision_id, d.symbol FROM decisions d
               LEFT JOIN decision_prices p ON p.decision_id = d.decision_id
               WHERE substr(d.run_at, 1, 10) = ? AND p.decision_id IS NULL""", (today,)).fetchall()
        if not rows:
            return 0
        symbols = sorted({r["symbol"] for r in rows})
        quotes = {}
        for i in range(0, len(symbols), MAX_SYMBOLS_PER_CALL):
            for q in aqua_client.get_prices(symbols[i:i + MAX_SYMBOLS_PER_CALL]):
                quotes[q["symbol"]] = q
        stamp = now.isoformat(timespec="seconds")
        written = 0
        for r in rows:
            q = quotes.get(r["symbol"])
            if q is None:
                log.warning("%s: 현재가 응답에 없음 — decision #%d 가격 기록 건너뜀", r["symbol"], r["decision_id"])
                continue
            conn.execute(
                "INSERT OR IGNORE INTO decision_prices (decision_id, price, price_time, recorded_at) VALUES (?,?,?,?)",
                (r["decision_id"], q.get("price"), q.get("timestamp"), stamp))
            written += 1
    log.info("판단 시점 가격 %d건 기록 (%d종목)", written, len(symbols))
    return written
