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
import math
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from threading import RLock
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from common.config import env, get_config
from common.logger import get_logger

BASE_URL = env("AQUA_BASE_URL")
TEAM_CODE = env("AQUA_TEAM_CODE")

# DRY_RUN: 켜져 있으면 place_order / cancel_order 가 실제 주문을 보내지 않고 로그만 남긴다.
# .env 에 DRY_RUN=0 이라고 명시한 곳(Google Cloud VM)에서만 실제 주문이 나간다. 값이 없으면 켜진 것으로 본다.
DRY_RUN = env("DRY_RUN", "1") != "0"

log = get_logger("aqua_client")
_dry_ids = itertools.count(1)

_token = None
_expires_at = None
_auth_lock = RLock()
_rate_lock = RLock()
_calls = deque()


def _wait_for_slot():
    """프로세스 내 모든 요청(로그인·재시도 포함)에 이동 60초 한도 적용."""
    limit = get_config()["api"]["max_calls_per_minute"]
    if type(limit) is not int or limit <= 0:
        raise ValueError("api.max_calls_per_minute must be a positive integer")
    while True:
        with _rate_lock:
            now = time.monotonic()
            while _calls and now - _calls[0] >= 60:
                _calls.popleft()
            if len(_calls) < limit:
                _calls.append(now)
                return
            delay = max(0.001, 60 - (now - _calls[0]))
        time.sleep(delay)


def _retry_delay(value):
    try:
        delay = float(value)
        return max(0, delay) if math.isfinite(delay) else 60
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60


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
    """공통 요청 함수.
    - 헤더 Authorization: Bearer {accessToken}. 토큰 만료 1시간 전이거나 401 이면 재로그인 후 1회 재시도
    - 호출 제한기: config api.max_calls_per_minute(80) 를 넘지 않게 대기
    - 에러 응답의 error.code / error.message 로 AquaError 를 만들어 raise
    - 429 RATE_LIMIT_EXCEEDED: Retry-After 헤더(초)만큼 쉬고 재시도
    - 502 MARKET_DATA_UNAVAILABLE: 몇 초 쉬고 1회 재시도
    - 그 밖의 4xx 는 재시도하지 않고 그대로 raise (특히 주문 API는 자동 재시도 금지 — 중복 주문 위험)
    - auth=False 인 호출(login, leaderboard)은 토큰 헤더 없이 보냄
    - 타임아웃·연결 오류는 서버 주소가 로그에 남지 않도록 AquaError(0, "NETWORK_ERROR") 로 바꿔 raise
      (주문 관리자는 이 코드를 "접수 여부 모름"으로 보고 주문 내역으로 확인함)"""
    if not BASE_URL or not BASE_URL.strip():
        raise ValueError("AQUA_BASE_URL is required")
    method = method.upper()
    # 조회 및 로그인만 재시도. 주문의 접수 여부가 불명확할 때 중복 전송하지 않는다.
    retryable = method == "GET" or (method == "POST" and path == "/api/auth/login")
    refreshed = rate_retried = market_retried = False
    while True:
        headers = {}
        if auth:
            with _auth_lock:
                if not _token or _expires_at is None or datetime.now(timezone.utc) >= _expires_at - timedelta(hours=1):
                    login()
                headers["Authorization"] = f"Bearer {_token}"
        _wait_for_slot()
        try:
            response = requests.request(
                method, BASE_URL.rstrip("/") + path, params=params, json=json,
                headers=headers, timeout=(5, 30), allow_redirects=False,
            )
        except requests.RequestException:
            # requests 예외에는 서버 주소가 포함될 수 있어 그대로 출력하지 않는다.
            raise AquaError(0, "NETWORK_ERROR", "API connection failed or timed out") from None
        try:
            data = response.json()
        except ValueError:
            raise AquaError(response.status_code, "INVALID_RESPONSE", "Expected JSON response") from None
        if not isinstance(data, dict):
            raise AquaError(response.status_code, "INVALID_RESPONSE", "Expected JSON object")
        if 200 <= response.status_code < 300:
            return data
        error = data.get("error") or {}
        if not isinstance(error, dict):
            error = {}
        code = error.get("code")
        if retryable and auth and response.status_code == 401 and code != "INVALID_TEAM_CODE" and not refreshed:
            login()
            refreshed = True
            continue
        if retryable and response.status_code == 429 and code == "RATE_LIMIT_EXCEEDED" and not rate_retried:
            time.sleep(_retry_delay(response.headers.get("Retry-After")))
            rate_retried = True
            continue
        if retryable and response.status_code == 502 and code == "MARKET_DATA_UNAVAILABLE" and not market_retried:
            time.sleep(2)
            market_retried = True
            continue
        raise AquaError(response.status_code, code, error.get("message", "API request failed"))


# ---------- 인증 ----------
def login() -> dict:
    """POST /api/auth/login {teamCode} → {accessToken, expiresAt, teamId, teamName}. 토큰 유효 24시간."""
    global _token, _expires_at
    if not TEAM_CODE or not TEAM_CODE.strip():
        raise ValueError("AQUA_TEAM_CODE is required")
    with _auth_lock:
        data = _request("POST", "/api/auth/login", json={"teamCode": TEAM_CODE}, auth=False)
        try:
            token = data["accessToken"]
            expiry = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
            if not isinstance(token, str) or not token or expiry.utcoffset() is None or expiry <= datetime.now(timezone.utc):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise AquaError(200, "INVALID_RESPONSE", "Invalid login token or expiry") from None
        _token, _expires_at = token, expiry
        return data


def me() -> dict:
    """GET /api/auth/me → {teamId, teamName}"""
    return _request("GET", "/api/auth/me")


# ---------- 종목 조회 ----------
def list_stocks(keyword: str | None = None) -> list[dict]:
    """GET /api/stocks?keyword= → [{symbol, name}]. keyword 생략 시 전체."""
    data = _request("GET", "/api/stocks", params={"keyword": keyword} if keyword is not None else None)
    if not isinstance(data.get("stocks"), list):
        raise AquaError(200, "INVALID_RESPONSE", "Missing stocks list")
    return data["stocks"]


def get_prices(symbols: list[str]) -> list[dict]:
    """GET /api/stocks/price?symbols=a,b → [{symbol, price, timestamp}]. 한 번에 최대 200개."""
    if not symbols:
        return []
    if len(symbols) > 200 or any(not isinstance(symbol, str) or not symbol for symbol in symbols):
        raise ValueError("Provide between 1 and 200 non-empty symbols")
    data = _request("GET", "/api/stocks/price", params={"symbols": ",".join(symbols)})
    if not isinstance(data.get("result"), list):
        raise AquaError(200, "INVALID_RESPONSE", "Missing price result list")
    return data["result"]


def get_orderbook(symbol: str) -> dict:
    """GET /api/stocks/{symbol}/orderbook → {asks:[{price,volume}] 낮은 가격부터, bids:[...] 높은 가격부터}"""
    if not symbol:
        raise ValueError("symbol is required")
    data = _request("GET", f"/api/stocks/{quote(symbol, safe='')}/orderbook")
    if not all(isinstance(data.get(key), list) for key in ("asks", "bids")):
        raise AquaError(200, "INVALID_RESPONSE", "Missing orderbook sides")
    return data


def get_candles(symbol: str, interval: str = "1d", count: int = 200, before: str | None = None) -> dict:
    """GET /api/stocks/{symbol}/candles → {candles:[{timestamp, openPrice, highPrice, lowPrice,
    closePrice, volume}], nextBefore}. count 최대 200, 더 과거는 nextBefore 를 before 로 넘겨 이어받기."""
    if interval not in {"1m", "1d"} or type(count) is not int or not 1 <= count <= 200:
        raise ValueError("interval must be 1m or 1d; count must be between 1 and 200")
    if not symbol:
        raise ValueError("symbol is required")
    params = {"interval": interval, "count": count}
    if before is not None:
        params["before"] = before
    data = _request("GET", f"/api/stocks/{quote(symbol, safe='')}/candles", params=params)
    if not isinstance(data.get("candles"), list):
        raise AquaError(200, "INVALID_RESPONSE", "Missing candles list")
    return data


# ---------- 계좌 ----------
def get_portfolio() -> dict:
    """GET /api/portfolio → {cash, availableCash, totalAssetValue, totalProfit,
    holdings:[{symbol, name, quantity, availableQuantity, avgBuyPrice, currentPrice}]}
    totalProfit 에는 수수료·세금이 이미 반영됨. 새 주문은 availableCash / availableQuantity 안에서만 가능."""
    data = _request("GET", "/api/portfolio")
    if not isinstance(data.get("holdings"), list):
        raise AquaError(200, "INVALID_RESPONSE", "Missing holdings list")
    return data


def get_orders(status: str | None = None, page: int = 0, size: int = 100) -> dict:
    """GET /api/orders?status=&page=&size= → {orders:[...], totalPages}. status: OPEN/PARTIAL/FILLED/CANCELLED"""
    if type(page) is not int or page < 0 or type(size) is not int or not 1 <= size <= 100:
        raise ValueError("page must be non-negative; size must be between 1 and 100")
    if status is not None and status not in {"OPEN", "PARTIAL", "FILLED", "CANCELLED"}:
        raise ValueError("Invalid order status")
    params = {"page": page, "size": size}
    if status is not None:
        params["status"] = status
    data = _request("GET", "/api/orders", params=params)
    if not isinstance(data.get("orders"), list) or type(data.get("totalPages")) is not int:
        raise AquaError(200, "INVALID_RESPONSE", "Missing orders or totalPages")
    return data


def get_limit() -> dict:
    """GET /api/orders/limit → {remainingTrades, resetAt, cooldowns:[{symbol, tradableAt}]}"""
    data = _request("GET", "/api/orders/limit")
    if not isinstance(data.get("cooldowns"), list) or type(data.get("remainingTrades")) is not int or not data.get("resetAt"):
        raise AquaError(200, "INVALID_RESPONSE", "Missing trade limit fields")
    return data


def get_leaderboard() -> dict:
    """GET /api/leaderboard → {updatedAt, baseline:{name, symbol:'069500', totalAssetValue, totalReturnRate},
    rankings:[{rank, teamId, teamName, totalAssetValue, totalReturnRate, rankChange}]}.
    토큰 불필요(auth=False). 15분마다 갱신. totalReturnRate 는 퍼센트(4.2 = +4.2%)."""
    data = _request("GET", "/api/leaderboard", auth=False)
    if not isinstance(data.get("rankings"), list) or not isinstance(data.get("baseline"), dict):
        raise AquaError(200, "INVALID_RESPONSE", "Missing leaderboard fields")
    return data


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
    _request 는 이 함수를 자동 재시도하지 않음(중복 주문). 응답이 없으면 호출한 쪽이 get_orders 로 확인.
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
