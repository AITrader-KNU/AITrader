"""4-5 청산 감시자 — 담당: 전략

읽는 것: 포트폴리오(평단가·현재가), positions_meta(매수일)
쓰는 테이블: sell_requests (why: STOP_LOSS / TAKE_PROFIT / EXPIRED, status='NEW')
수치는 config.yaml 의 exit 섹션에서만 읽기

가격 규칙만 봅니다. 악재·LLM 매도 판단은 판단 에이전트가 decisions 에 SELL 로 쓰고,
주문 관리자가 처리합니다.
"""
from common.logger import get_logger

log = get_logger("exit_monitor")


def run() -> None:
    """보유 종목마다 손절·익절·보유 기간을 확인해 sell_requests 에 매도 요청을 쓴다.
    - 손익률은 (현재가 - 평단가) / 평단가. 실제로는 왕복 수수료·세금 약 0.23%가 더 빠진다
    - 보유 기간은 positions_meta.bought_at 부터 센 거래일 수(주말·config 휴장일 제외)
    - 같은 종목에 처리 안 된(NEW) 요청이 이미 있으면 또 쓰지 않기
    - 쿨다운 중이어도 요청은 써 둔다. 주문 관리자가 락이 풀린 뒤 처리
    TODO(전략)"""
    log.info("exit_monitor.run: TODO")
