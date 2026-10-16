"""4-5 청산 감시자 — 담당: 전략

읽는 것: 포트폴리오(평단가·현재가), positions_meta(매수일), decisions(bad_news)
쓰는 테이블: sell_requests (why: STOP_LOSS / TAKE_PROFIT / EXPIRED / BAD_NEWS, status='NEW')
수치는 config.yaml 의 exit 섹션에서만 읽기
"""
from common.logger import get_logger

log = get_logger("exit_monitor")


def run() -> None:
    """보유 종목마다 손절·익절·보유 기간·악재를 확인해 sell_requests 에 매도 요청을 쓴다.
    - 같은 종목에 처리 안 된(NEW) 요청이 이미 있으면 또 쓰지 않기
    - 쿨다운 중이면 요청만 남겨 두고, 주문 관리자가 풀린 뒤 처리
    TODO(전략)"""
    log.info("exit_monitor.run: TODO")
