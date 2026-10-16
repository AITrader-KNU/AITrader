"""4-2 후보 선정기 — 담당: 전략

읽는 테이블: daily_candles
쓰는 테이블: candidates (run_date 하루치 top_n 개)
수치는 config.yaml 의 universe / candidates 섹션에서만 읽기
"""
from datetime import date

from common.logger import get_logger

log = get_logger("candidate_selector")


def run(run_date: date | None = None) -> None:
    """유동성 필터 → 추세 조건(20일선) → 모멘텀 점수 → 과열 제외 → 상위 top_n 을 candidates 에 저장.
    TODO(전략)"""
    run_date = run_date or date.today()
    log.info("candidate_selector.run(%s): TODO", run_date)
