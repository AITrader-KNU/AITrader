"""4-4 주문·리스크 관리자 — 담당: 팀장

실제 주문을 내는 곳은 여기 하나뿐입니다.
읽는 테이블: sell_requests, decisions (status='NEW' 만)
쓰는 테이블: orders_log, positions_meta, 그리고 처리한 요청의 status 를 DONE / SKIPPED 로 변경

처리 순서
  1) sell_requests(NEW)               → 보유 중이면 시장가 매도
  2) decisions 의 SELL(NEW)           → 보유 중이면 시장가 매도, 아니면 SKIPPED
  3) decisions 의 BUY(NEW), 모멘텀 순위순 → 매수 검사 통과 시 시장가 매수
  4) decisions 의 WAIT(NEW)           → 바로 SKIPPED

매수 검사 (하나라도 걸리면 SKIPPED)
  buy_cutoff(14:00) 이전 / bad_news 아님 / 이미 보유한 종목 아님 / 보유 max_positions 미만 /
  남은 거래 횟수 있음 / 수량 1주 이상 (plan_market_buy: 비중 상한·availableCash·호가 잔량·수수료 반영)

API 오류 처리
  SYMBOL_COOLDOWN       → 요청을 NEW 로 둠(락 풀린 뒤 다음 사이클에 재처리)
  TRADE_LIMIT_EXCEEDED  → 요청을 NEW 로 두고 이번 시간 남은 주문 중단(resetAt 이후 재개)
  NO_LIQUIDITY, INSUFFICIENT_CASH, INSUFFICIENT_QUANTITY, UNKNOWN_SYMBOL → SKIPPED + 로그
  응답 없음(타임아웃)     → get_orders 로 접수 여부 확인 후에만 재시도(중복 주문 방지)

체결 후
  orders_log 에 fee·tax 까지 기록 / 매수 체결 시 positions_meta 추가 /
  전량 매도 시 positions_meta 에서 삭제 / 요청 status 를 곧바로 DONE
"""
import math

from common.config import get_config
from common.logger import get_logger

log = get_logger("order_manager")


def plan_market_buy(budget: int, asks: list[dict], commission: float | None = None) -> tuple[int, int]:
    """시장가 매수로 budget 원 안에서 살 수 있는 수량과 예상 체결 금액.

    asks: 호가 API 의 매도호가 [{price, volume}, ...] (낮은 가격부터).
    시장가는 낮은 호가부터 잔량만큼 체결되므로 호가를 순서대로 훑으며 계산한다.
    수수료(체결 금액 × commission, 1원 미만 절사)까지 budget 안에 들어오게 한다.
    budget 은 보통 min(총자산 × max_weight_per_stock, availableCash).
    반환: (수량, 수수료 제외 예상 체결 금액). 살 수 없으면 (0, 0).
    """
    if commission is None:
        commission = get_config()["fees"]["commission"]
    qty, cost = 0, 0
    for level in asks:
        price, volume = int(level["price"]), int(level["volume"])
        if price <= 0 or volume <= 0:
            continue
        room = budget / (1 + commission) - cost
        k = min(volume, int(room // price))
        if k <= 0:
            break
        qty, cost = qty + k, cost + k * price
        if k < volume:          # 이 호가에서 예산이 끝남
            break
    while qty > 0 and cost + math.floor(cost * commission) > budget:   # 절사 오차 보정
        last = cost // qty
        qty, cost = qty - 1, cost - last
    return qty, cost


def run() -> None:
    """위 처리 순서대로 요청을 처리한다. TODO(팀장)"""
    log.info("order_manager.run: TODO")
