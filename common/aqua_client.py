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

DRY_RUN = env("DRY_RUN", "1") != "0"

log = get_logger("aqua_client")
_dry_ids = itertools.count(1)

_token = None
_expires_at = None
_auth_lock = RLock()
_rate_lock = RLock()
_calls = deque()


def _wait_for_slot():
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

    def __init__(self, status: int, code: str | None, message: str = ""):
        super().__init__(f"{status} {code} {message}")
        self.status, self.code = status, code


def _request(method: str, path: str, *, params=None, json=None, auth=True) -> dict:
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


def login() -> dict:
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
    return _request("GET", "/api/auth/me")


def list_stocks(keyword: str | None = None) -> list[dict]:
    data = _request("GET", "/api/stocks", params={"keyword": keyword} if keyword is not None else None)
    if not isinstance(data.get("stocks"), list):
        raise AquaError(200, "INVALID_RESPONSE", "Missing stocks list")
    return data["stocks"]


def get_prices(symbols: list[str]) -> list[dict]:
    if not symbols:
        return []
    if len(symbols) > 200 or any(not isinstance(symbol, str) or not symbol for symbol in symbols):
        raise ValueError("Provide between 1 and 200 non-empty symbols")
    data = _request("GET", "/api/stocks/price", params={"symbols": ",".join(symbols)})
    if not isinstance(data.get("result"), list):
        raise AquaError(200, "INVALID_RESPONSE", "Missing price result list")
    return data["result"]


def get_orderbook(symbol: str) -> dict:
    if not symbol:
        raise ValueError("symbol is required")
    data = _request("GET", f"/api/stocks/{quote(symbol, safe='')}/orderbook")
    if not all(isinstance(data.get(key), list) for key in ("asks", "bids")):
        raise AquaError(200, "INVALID_RESPONSE", "Missing orderbook sides")
    return data


def get_candles(symbol: str, interval: str = "1d", count: int = 200, before: str | None = None) -> dict:
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


def get_portfolio() -> dict:
    data = _request("GET", "/api/portfolio")
    if not isinstance(data.get("holdings"), list):
        raise AquaError(200, "INVALID_RESPONSE", "Missing holdings list")
    return data


def get_orders(status: str | None = None, page: int = 0, size: int = 100) -> dict:
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
    data = _request("GET", "/api/orders/limit")
    if not isinstance(data.get("cooldowns"), list) or type(data.get("remainingTrades")) is not int or not data.get("resetAt"):
        raise AquaError(200, "INVALID_RESPONSE", "Missing trade limit fields")
    return data


def get_leaderboard() -> dict:
    data = _request("GET", "/api/leaderboard", auth=False)
    if not isinstance(data.get("rankings"), list) or not isinstance(data.get("baseline"), dict):
        raise AquaError(200, "INVALID_RESPONSE", "Missing leaderboard fields")
    return data


def place_order(symbol: str, side: str, quantity: int, price: int | None = None) -> dict:
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
    if DRY_RUN:
        log.warning("[DRY_RUN] 취소 안 보냄: orderId=%s", order_id)
        return {"orderId": order_id, "status": "CANCELLED", "dryRun": True}
    log.info("주문 취소: orderId=%s", order_id)
    return _request("DELETE", f"/api/orders/{order_id}")
