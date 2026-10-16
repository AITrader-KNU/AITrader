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
"""
from common.config import env, get_config

BASE_URL = env("AQUA_BASE_URL")
TEAM_CODE = env("AQUA_TEAM_CODE")


class AquaError(Exception):
    """AQuA 가 4xx/5xx 를 돌려준 경우. code 예: RATE_LIMIT_EXCEEDED, SYMBOL_COOLDOWN, ORDER_NOT_OPEN"""

    def __init__(self, status: int, code: str | None, message: str = ""):
        super().__init__(f"{status} {code} {message}")
        self.status, self.code = status, code


def _request(method: str, path: str, *, params=None, json=None, auth=True) -> dict:
    """공통 요청 함수. TODO(데이터·인프라): 토큰 관리, 호출 제한기, 429 재시도."""
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
    holdings:[{symbol, name, quantity, availableQuantity, avgBuyPrice, currentPrice}]}"""
    raise NotImplementedError


def get_orders(status: str | None = None, page: int = 0, size: int = 100) -> dict:
    """GET /api/orders?status=&page=&size= → {orders:[...], totalPages}. status: OPEN/PARTIAL/FILLED/CANCELLED"""
    raise NotImplementedError


def get_limit() -> dict:
    """GET /api/orders/limit → {remainingTrades, resetAt, cooldowns:[{symbol, tradableAt}]}"""
    raise NotImplementedError


def get_leaderboard() -> dict:
    """GET /api/leaderboard → {updatedAt, baseline:{KODEX 200}, rankings:[...]}. 15분마다 갱신."""
    raise NotImplementedError


# ---------- 주문 (팀장 / order_manager 전용) ----------
def place_order(symbol: str, side: str, quantity: int, price: int | None = None) -> dict:
    """POST /api/orders {symbol, order_code: BUY|SELL, quantity, price?}
    price 생략 = 시장가(호가 잔량만큼 즉시 체결, 남은 수량 자동 취소). 주문 즉시 그 종목 30분 락.
    응답: {orderId, status, filledQuantity, fills, executedPrice, remainingTrades, ...}
    TODO(팀장)"""
    raise NotImplementedError


def cancel_order(order_id: int) -> dict:
    """DELETE /api/orders/{orderId} — OPEN/PARTIAL 만 취소 가능(아니면 422 ORDER_NOT_OPEN). TODO(팀장)"""
    raise NotImplementedError
