"""임시 판단 규칙 — LLM 판단 에이전트가 연결되기 전까지 쓰는 자리 채우기. 담당: 팀장

config.yaml 의 llm.use_llm 이 false 이면 main.py 가 news_judge.judge.run() 대신 이 run() 을 부른다.
오늘 후보(candidates) 중 모멘텀 순위 상위 N개(llm.temp_rule_top_n)를 BUY 로 decisions 에 쓴다.
같은 날 이미 판단이 있는 종목은 다시 쓰지 않는다. 출력 형식은 판단 에이전트와 똑같으므로
주문 관리자는 둘을 구분하지 않는다. 판단 에이전트가 완성되면 use_llm 을 true 로 바꾸기만 하면 된다.
"""
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
