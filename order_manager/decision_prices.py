from datetime import datetime
from zoneinfo import ZoneInfo

from common import aqua_client
from common.config import get_config
from common.db import connect
from common.logger import get_logger

log = get_logger("decision_prices")

MAX_SYMBOLS_PER_CALL = 200


def run(now: datetime | None = None) -> int:
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
