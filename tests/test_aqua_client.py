from datetime import datetime, timedelta, timezone

import pytest
import requests

from common import aqua_client as api


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(api, "BASE_URL", "https://example.invalid")
    monkeypatch.setattr(api, "TEAM_CODE", "test-team")
    monkeypatch.setattr(api, "_token", "test-token")
    monkeypatch.setattr(api, "_expires_at", datetime.now(timezone.utc) + timedelta(hours=24))
    api._calls.clear()
    yield
    api._calls.clear()


def responses(monkeypatch, *items):
    calls = []
    queue = iter(items)

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        status, body, headers = item
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.json = lambda: body
        return response

    monkeypatch.setattr(api.requests, "request", request)
    return calls


def ok(body):
    return 200, body, {}


def login_response():
    return ok({"accessToken": "renewed", "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()})


@pytest.mark.parametrize("minutes", [None, 30])
def test_login_before_authenticated_read(monkeypatch, minutes):
    if minutes is None:
        monkeypatch.setattr(api, "_token", None)
    else:
        monkeypatch.setattr(api, "_expires_at", datetime.now(timezone.utc) + timedelta(minutes=minutes))
    calls = responses(monkeypatch, login_response(), ok({"teamId": 1}))
    assert api.me() == {"teamId": 1}
    assert calls[0][2]["headers"] == {}
    assert calls[0][2]["json"] == {"teamCode": "test-team"}
    assert calls[1][2]["headers"]["Authorization"] == "Bearer renewed"
    assert len(api._calls) == 2


def test_401_refresh_only_once(monkeypatch):
    unauthorized = (401, {"error": {"code": "UNAUTHORIZED"}}, {})
    calls = responses(monkeypatch, unauthorized, login_response(), unauthorized)
    with pytest.raises(api.AquaError) as exc:
        api.me()
    assert exc.value.status == 401
    assert len(calls) == 3


def test_401_without_error_code_refreshes(monkeypatch):
    calls = responses(monkeypatch, (401, {}, {}), login_response(), ok({"teamId": 1}))
    assert api.me()["teamId"] == 1
    assert len(calls) == 3


def test_rate_limit_retry_succeeds_and_counts_every_call(monkeypatch):
    sleeps = []
    monkeypatch.setattr(api.time, "sleep", sleeps.append)
    calls = responses(monkeypatch,
        (429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}}, {"Retry-After": "4"}),
        ok({"teamId": 1}))
    assert api.me()["teamId"] == 1
    assert sleeps == [4]
    assert len(calls) == len(api._calls) == 2


def test_retry_after_invalid_and_http_date():
    from email.utils import format_datetime
    assert api._retry_delay("invalid") == 60
    assert api._retry_delay("nan") == 60
    assert api._retry_delay("-1") == 0
    future = datetime.now(timezone.utc) + timedelta(seconds=20)
    assert 18 <= api._retry_delay(format_datetime(future)) <= 20


def test_configured_eighty_call_limit(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(api, "get_config", lambda: {"api": {"max_calls_per_minute": 80}})
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(api.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    for _ in range(80):
        api._wait_for_slot()
    assert clock[0] == 0
    api._wait_for_slot()
    assert clock[0] == 60


@pytest.mark.parametrize("status,code,delay", [(429, "RATE_LIMIT_EXCEEDED", 3), (502, "MARKET_DATA_UNAVAILABLE", 2)])
def test_transient_error_retries_once(monkeypatch, status, code, delay):
    sleeps = []
    monkeypatch.setattr(api.time, "sleep", sleeps.append)
    error = (status, {"error": {"code": code}}, {"Retry-After": "3"})
    calls = responses(monkeypatch, error, error)
    with pytest.raises(api.AquaError):
        api.me()
    assert len(calls) == 2
    assert sleeps == [delay]


@pytest.mark.parametrize("status,code", [(401, "UNAUTHORIZED"), (429, "RATE_LIMIT_EXCEEDED"), (502, "MARKET_DATA_UNAVAILABLE")])
def test_mutation_never_retried(monkeypatch, status, code):
    calls = responses(monkeypatch, (status, {"error": {"code": code}}, {}))
    with pytest.raises(api.AquaError):
        api._request("POST", "/api/orders", json={})
    assert len(calls) == 1


def test_network_error_hides_url(monkeypatch):
    calls = responses(monkeypatch, requests.Timeout("private-server-address"))
    with pytest.raises(api.AquaError) as exc:
        api.me()
    assert "private-server-address" not in str(exc.value)
    assert len(calls) == 1


def test_stocks_and_pagination(monkeypatch):
    cursor = "2026-01-01T00:00:00+09:00"
    calls = responses(monkeypatch, ok({"stocks": [{"symbol": "005930"}]}),
                      ok({"candles": [{"timestamp": "first"}], "nextBefore": cursor}),
                      ok({"candles": [{"timestamp": "older"}], "nextBefore": None}))
    assert api.list_stocks()[0]["symbol"] == "005930"
    first = api.get_candles("005930")
    api.get_candles("005930", before=first["nextBefore"])
    assert calls[2][2]["params"]["before"] == cursor
    assert calls[2][2]["timeout"] == (5, 30)
    prepared = requests.Request("GET", calls[2][1], params=calls[2][2]["params"]).prepare()
    assert "%2B09%3A00" in prepared.url


def test_sliding_rate_limit(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(api, "get_config", lambda: {"api": {"max_calls_per_minute": 2}})
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(api.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    for _ in range(3):
        api._wait_for_slot()
    assert clock[0] == 60
    assert list(api._calls) == [60]


@pytest.mark.parametrize("limit", [0, -1, 0.5, True, "80"])
def test_invalid_rate_limit_fails_before_request(monkeypatch, limit):
    monkeypatch.setattr(api, "get_config", lambda: {"api": {"max_calls_per_minute": limit}})
    monkeypatch.setattr(api.requests, "request", lambda *a, **k: pytest.fail("Unexpected request"))
    with pytest.raises(ValueError, match="positive integer"):
        api.me()


def test_invalid_login_expiry(monkeypatch):
    responses(monkeypatch, ok({"accessToken": "token", "expiresAt": "2030-01-01T00:00:00"}))
    with pytest.raises(api.AquaError, match="INVALID_RESPONSE"):
        api.login()


def test_missing_list_is_not_silently_empty(monkeypatch):
    responses(monkeypatch, ok({}))
    with pytest.raises(api.AquaError, match="INVALID_RESPONSE"):
        api.list_stocks()


def test_portfolio_holdings(monkeypatch):
    responses(monkeypatch, ok({"holdings": [{"symbol": "005930", "quantity": 1}]}), ok({}))
    assert api.get_portfolio()["holdings"][0]["symbol"] == "005930"
    with pytest.raises(api.AquaError, match="INVALID_RESPONSE"):
        api.get_portfolio()


def test_price_batch_and_limit(monkeypatch):
    calls = responses(monkeypatch, ok({"result": [{"symbol": "005930", "price": 100}]}))
    assert api.get_prices([]) == []
    assert api.get_prices(["005930", "000660"])[0]["price"] == 100
    assert calls[0][2]["params"] == {"symbols": "005930,000660"}
    with pytest.raises(ValueError):
        api.get_prices(["005930"] * 201)
    assert len(calls) == 1


def test_orderbook_and_orders_parameters(monkeypatch):
    calls = responses(monkeypatch, ok({"asks": [], "bids": []}), ok({"orders": [], "totalPages": 0}))
    assert api.get_orderbook("005930") == {"asks": [], "bids": []}
    api.get_orders(status="OPEN", page=2, size=25)
    assert calls[1][2]["params"] == {"status": "OPEN", "page": 2, "size": 25}
    for kwargs in ({"page": -1}, {"size": 101}, {"status": "NEW"}, {"page": 0.5}):
        with pytest.raises(ValueError):
            api.get_orders(**kwargs)


def test_limit_and_public_leaderboard(monkeypatch):
    limit = {"remainingTrades": 30, "resetAt": "2026-10-22T10:00:00+09:00", "cooldowns": []}
    calls = responses(monkeypatch, ok(limit), ok({"rankings": [], "baseline": {}}))
    assert api.get_limit() == limit
    monkeypatch.setattr(api, "_token", None)
    api.get_leaderboard()
    assert calls[1][2]["headers"] == {}


@pytest.mark.parametrize("function,args", [(api.get_prices, (["005930"],)),
                         (api.get_orderbook, ("005930",)), (api.get_orders, ()),
                         (api.get_limit, ()), (api.get_leaderboard, ())])
def test_missing_query_fields_raise(monkeypatch, function, args):
    responses(monkeypatch, ok({}))
    with pytest.raises(api.AquaError, match="INVALID_RESPONSE"):
        function(*args)
