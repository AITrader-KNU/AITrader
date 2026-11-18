from datetime import datetime
from zoneinfo import ZoneInfo

from common.config import get_config
from common.db import connect
from common.logger import get_logger

log = get_logger("temp_rule")


def run(now: datetime | None = None) -> int:
    cfg = get_config()
    now = now or datetime.now(ZoneInfo(cfg["timezone"]))
    today = now.date().isoformat()
    top_n = cfg["llm"]["temp_rule_top_n"]

    with connect() as conn:
        rows = conn.execute(
            """SELECT symbol FROM (SELECT symbol FROM candidates
                                   WHERE run_date = ? AND momentum_rank IS NOT NULL
                                   ORDER BY momentum_rank LIMIT ?)
               WHERE symbol NOT IN (SELECT symbol FROM decisions WHERE substr(run_at, 1, 10) = ?)""",
            (today, top_n, today)).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO decisions (run_at, symbol, action, bad_news, reason) VALUES (?, ?, 'BUY', 0, ?)",
                (now.isoformat(timespec="seconds"), r["symbol"], f"임시 규칙: 후보 상위 {top_n}"))
    log.info("임시 규칙: BUY %d건 기록", len(rows))
    return len(rows)
