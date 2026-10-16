"""4-4 주문·리스크 관리자 — 담당: 팀장

실제 주문을 내는 곳은 여기 하나뿐입니다.
읽는 테이블: sell_requests, decisions (status='NEW' 만)
쓰는 테이블: orders_log, positions_meta, 그리고 처리한 요청의 status 를 DONE / SKIPPED 로 변경
"""
from common.logger import get_logger

log = get_logger("order_manager")


def run() -> None:
    """1) sell_requests(NEW) 먼저 처리 → 2) decisions 의 BUY(NEW) 를 모멘텀 순위순으로 처리.
    매수 검사: buy_cutoff 이전 / 보유 max_positions 미만 / 종목당 max_weight_per_stock 이내 /
             availableCash 충분 / 남은 거래 횟수 / 쿨다운 아님 / 호가 매도 잔량 이내 수량
    처리한 요청은 곧바로 status 를 바꿔 중복 주문 방지.
    응답이 없으면 get_orders 로 접수 여부를 확인한 뒤에만 재시도.
    TODO(팀장)"""
    log.info("order_manager.run: TODO")
