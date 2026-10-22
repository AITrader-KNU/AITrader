"""AQuA API 클라이언트 — 모든 AQuA 호출은 이 파일의 함수로만 합니다.

담당: 데이터·인프라 (주문 함수 place_order / cancel_order 는 팀장)
1주차에 할 일
  - _request(): 토큰 자동 갱신(만료 1시간 전 또는 401 응답 시 재로그인)
  - _request(): 호출 제한기(config api.max_calls_per_minute), 429 응답 시 대기 후 재시도
  - 아래 함수들의 본문 채우기

규칙
  - 주문 API(place_order, cancel_order)는 order_manager 만 호출합니다.
    다른 사람은 테스트로도 부르지 않습니다(거래 횟수·30분 락을 팀이 공유).
  - 모든 응답은 JSON, 시각은 ISO-8601 (KST, +09:00).
  - 전체 API 설명은 docs/API.md
"""
import itertools
from datetime import datetime
from zoneinfo import ZoneInfo

from common.config import env, get_config
from common.logger import get_logger

BASE_URL = env("AQUA_BASE_URL")
TEAM_CODE = env("AQUA_TEAM_CODE")

# DRY_RUN: 켜져 있으면 place_order / cancel_order 가 실제 주문을 보내지 않고 로그만 남긴다.
# .env 에 DRY_RUN=0 이라고 명시한 곳(학교 서버)에서만 실제 주문이 나간다. 값이 없으면 켜진 것으로 본다.
DRY_RUN = env("DRY_RUN", "1") != "0"

log = get_logger("aqua_client")
_dry_ids = itertools.count(1)


class AquaError(Exception):
    """AQuA 가 4xx/5xx 를 돌려준 경우. 에러 응답은 항상 {"error": {"code": ..., "message": ...}} 형태.

    status  code                     의미 / 처리
    400     INVALID_REQUEST          파라미터 오류 → 코드 버그, 로그 남기기
    401     UNAUTHORIZED             토큰 만료·무효 → _request 가 재로그인 후 1회 재시도
    401     INVALID_TEAM_CODE        .env 의 팀 코드 확인
    404     UNKNOWN_SYMBOL           코스피 보통주가 아닌 종목
    404     ORDER_NOT_FOUND          내 주문이 아니거나 없는 번호
    422     MARKET_CLOSED            장 시간(09:00~15:00) 외
    422     INSUFFICIENT_CASH        현금 부족 (대기 주문에 묶인 현금 제외 기준)
    422     INSUFFICIENT_QUANTITY    매도 가능 수량 부족
    422     NO_LIQUIDITY             시장가인데 닿는 호가가 없음
    422     ORDER_NOT_OPEN           이미 체결·취소된 주문을 취소
    429     TRADE_LIMIT_EXCEEDED     이번 시간 거래 횟수 소진 → resetAt(다음 정각)까지 주문 중단
    429     SYMBOL_COOLDOWN          30분 락 → 메시지의 'tradable again at' 이후 재시도
    429     RATE_LIMIT_EXCEEDED      분당 호출 한도 → _request 가 Retry-After 만큼 쉬고 재시도
    502     MARKET_DATA_UNAVAILABLE  시세 제공처 장애 → _request 가 잠시 후 1회 재시도
    """

    def __init__(self, status: int, code: str | None, message: str = ""):
        super().__init__(f"{status} {code} {message}")
        self.status, self.code = status, code


def _request(method: str, path: str, *, params=None, json=None, auth=True) -> dict:
    """공통 요청 함수. TODO(데이터·인프라)
    - 헤더 Authorization: Bearer {accessToken}. 토큰 만료 1시간 전이거나 401 이면 재로그인 후 1회 재시도
    - 호출 제한기: config api.max_calls_per_minute(80) 를 넘지 않게 대기
    - 에러 응답의 error.code / error.message 로 AquaError 를 만들어 raise
    - 429 RATE_LIMIT_EXCEEDED: Retry-After 헤더(초)만큼 쉬고 재시도
    - 502 MARKET_DATA_UNAVAILABLE: 몇 초 쉬고 1회 재시도
    - 그 밖의 4xx 는 재시도하지 않고 그대로 raise (특히 주문 API는 자동 재시도 금지 — 중복 주문 위험)
    - 타임아웃·연결 오류는 requests 예외(requests.RequestException)를 감싸지 말고 그대로 raise
      (주문 관리자가 이 예외를 보고 주문 내역으로 접수 여부를 확인함)
    - auth=False 인 호출(login, leaderboard)은 토큰 헤더 없이 보냄"""
    raise NotImplementedError


# ---------- 인증 ----------
def login() -> dict:
    """POST /api/auth/login {teamCode} → {accessToken, expiresAt, teamId, teamName}. 토큰 유효 24시간."""
    raise NotImplementedError


def me() -> dict:
    """GET /api/auth/me → {teamId, teamName}"""
    raise NotImplementedError


# ---------- 종목 조회 ----------
def list_stocks(keyword: str | None = None) -> list[dict]:
    """GET /api/stocks?keyword= → [{symbol, name}]. keyword 생략 시 전체."""
    raise NotImplementedError


def get_prices(symbols: list[str]) -> list[dict]:
    """GET /api/stocks/price?symbols=a,b → [{symbol, price, timestamp}]. 한 번에 최대 200개."""
    raise NotImplementedError


def get_orderbook(symbol: str) -> dict:
    """GET /api/stocks/{symbol}/orderbook → {asks:[{price,volume}] 낮은 가격부터, bids:[...] 높은 가격부터}"""
    raise NotImplementedError


def get_candles(symbol: str, interval: str = "1d", count: int = 200, before: str | None = None) -> dict:
    """GET /api/stocks/{symbol}/candles → {candles:[{timestamp, openPrice, highPrice, lowPrice,
    closePrice, volume}], nextBefore}. count 최대 200, 더 과거는 nextBefore 를 before 로 넘겨 이어받기."""
    raise NotImplementedError


# ---------- 계좌 ----------
def get_portfolio() -> dict:
    """GET /api/portfolio → {cash, availableCash, totalAssetValue, totalProfit,
    holdings:[{symbol, name, quantity, availableQuantity, avgBuyPrice, currentPrice}]}
    totalProfit 에는 수수료·세금이 이미 반영됨. 새 주문은 availableCash / availableQuantity 안에서만 가능."""
    raise NotImplementedError


def get_orders(status: str | None = None, page: int = 0, size: int = 100) -> dict:
    """GET /api/orders?status=&page=&size= → {orders:[...], totalPages}. status: OPEN/PARTIAL/FILLED/CANCELLED"""
    raise NotImplementedError


def get_limit() -> dict:
    """GET /api/orders/limit → {remainingTrades, resetAt, cooldowns:[{symbol, tradableAt}]}"""
    raise NotImplementedError


def get_leaderboard() -> dict:
    """GET /api/leaderboard → {updatedAt, baseline:{name, symbol:'069500', totalAssetValue, totalReturnRate},
    rankings:[{rank, teamId, teamName, totalAssetValue, totalReturnRate, rankChange}]}.
    토큰 불필요(auth=False). 15분마다 갱신. totalReturnRate 는 퍼센트(4.2 = +4.2%)."""
    raise NotImplementedError


# ---------- 주문 (팀장 / order_manager 전용) ----------
def place_order(symbol: str, side: str, quantity: int, price: int | None = None) -> dict:
    """POST /api/orders {symbol, order_code: BUY|SELL, quantity, price?}
    - price 생략 = 시장가: 지금 호가 잔량만큼 즉시 체결, 남은 수량은 버려짐(CANCELLED).
      닿는 호가가 하나도 없으면 422 NO_LIQUIDITY.
    - price 지정 = 지정가: 남은 수량은 OPEN/PARTIAL 로 대기, 서버가 15분 틱마다 재시도. 취소 전까지 유효.
    - 주문 1건 = 거래 횟수 1회 (거절된 주문, 틱 체결, 취소는 소모하지 않음)
    - 주문을 넣는 순간(체결 여부와 무관) 그 종목 30분 락. 틱에서 체결되면 30분을 다시 셈.
    - 수수료: 체결 금액의 0.015% (매수·매도), 매도 세금 0.20%. 1원 미만 절사.
    응답: {orderId, symbol, order_code, price, quantity, filledQuantity, status, fills:[{price, quantity,
          executedAt}], executedPrice, totalAmount, fee, tax, remainCash, availableCash, remainingTrades,
          createdAt, executedAt}
    _request 가 이 함수를 자동 재시도하면 안 됨(중복 주문). 응답이 없으면 호출한 쪽이 get_orders 로 확인.
    DRY_RUN 이면 주문을 보내지 않고, 전량 체결된 것처럼 꾸민 응답을 돌려준다(orderId 는 음수)."""
    side = side.upper()
    if side not in ("BUY", "SELL") or quantity <= 0:
        raise ValueError(f"잘못된 주문: {side} {quantity}")
    body = {"symbol": symbol, "order_code": side, "quantity": int(quantity)}
    if price is not None:
        body["price"] = int(price)

    if DRY_RUN:
        log.warning("[DRY_RUN] 주문 안 보냄: %s", body)
        now = datetime.now(ZoneInfo(get_config()["timezone"])).isoformat(timespec="seconds")
        return {"orderId": -next(_dry_ids), "symbol": symbol, "order_code": side, "price": price,
                "quantity": quantity, "filledQuantity": quantity, "status": "FILLED", "fills": [],
                "executedPrice": price, "totalAmount": None, "fee": None, "tax": None,
                "remainingTrades": None, "createdAt": now, "executedAt": now, "dryRun": True}

    log.info("주문: %s", body)
    return _request("POST", "/api/orders", json=body)


def cancel_order(order_id: int) -> dict:
    """DELETE /api/orders/{orderId} — OPEN/PARTIAL 만 취소 가능(아니면 422 ORDER_NOT_OPEN,
    내 주문이 아니면 404 ORDER_NOT_FOUND). 묶인 현금·주식이 풀림. 거래 횟수를 쓰지 않음.
    응답은 주문 내역 한 건과 같은 형태(status=CANCELLED)."""
    if DRY_RUN:
        log.warning("[DRY_RUN] 취소 안 보냄: orderId=%s", order_id)
        return {"orderId": order_id, "status": "CANCELLED", "dryRun": True}
    log.info("주문 취소: orderId=%s", order_id)
    return _request("DELETE", f"/api/orders/{order_id}")
