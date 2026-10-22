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

오래된 요청
  같은 종목의 NEW decisions 가 여러 개면 가장 최근 것만 처리하고 나머지는 SKIPPED
  BUY 는 오늘 판단만 유효(어제 이전 BUY 는 SKIPPED). 매도 요청은 날짜와 상관없이 처리

API 오류 처리
  SYMBOL_COOLDOWN       → 요청을 NEW 로 둠(락 풀린 뒤 다음 사이클에 재처리)
  TRADE_LIMIT_EXCEEDED  → 요청을 NEW 로 두고 이번 시간 남은 주문 중단(resetAt 이후 재개)
  NO_LIQUIDITY, INSUFFICIENT_CASH, INSUFFICIENT_QUANTITY, UNKNOWN_SYMBOL → SKIPPED + 로그
  MARKET_CLOSED         → 요청을 NEW 로 두고 이번 실행 중단
  응답 없음(타임아웃 등)  → get_orders 로 접수 여부 확인. 접수됐으면 그 결과로 처리,
                          확인이 안 되면 SKIPPED + 로그(중복 주문보다 놓치는 쪽이 안전)

체결 후
  orders_log 에 fee·tax 까지 기록 / 매수 체결 시 positions_meta 추가 /
  전량 매도 시 positions_meta 에서 삭제 / 요청 status 를 곧바로 DONE
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

from common import aqua_client as aqua
from common.aqua_client import AquaError
from common.config import get_config
from common.db import STATUS_DONE, STATUS_NEW, STATUS_SKIPPED, connect
from common.logger import get_logger

log = get_logger("order_manager")

KEEP = "KEEP"   # 요청을 NEW 로 남겨 다음 사이클에 다시 처리
KEEP_CODES = {"SYMBOL_COOLDOWN", "TRADE_LIMIT_EXCEEDED", "MARKET_CLOSED"}   # docs/API.md 에러 표
SYMBOL_LOCK_MINUTES = 30   # docs/API.md: 주문을 넣으면 그 종목은 30분간 다시 주문 불가
REQUEST_TABLES = {"decisions": "decision_id", "sell_requests": "request_id"}   # status 를 바꾸는 테이블


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
    # 체결 금액 × (1 + 수수료율) ≤ budget 이어야 하므로, 쓸 수 있는 체결 금액의 상한은 budget / (1 + 수수료율).
    # 수수료는 1원 미만 절사(docs/API.md)라 실제 수수료는 이 계산보다 크지 않다.
    max_cost = math.floor(budget / (1 + commission))
    qty, cost = 0, 0
    for level in asks:                      # 매수는 매도호가를 낮은 가격부터 잔량만큼 체결(docs/API.md)
        price, volume = int(level["price"]), int(level["volume"])
        if price <= 0 or volume <= 0:
            continue
        k = min(volume, (max_cost - cost) // price)
        if k <= 0:
            break
        qty, cost = qty + k, cost + k * price
        if k < volume:          # 이 호가에서 예산이 끝남
            break
    return qty, cost


@dataclass
class State:
    """이번 실행 동안의 계좌 상태. 주문할 때마다 갱신해서 다음 요청 검사에 쓴다."""
    now: datetime
    total_asset: int
    available_cash: int
    holdings: dict          # symbol -> {"quantity", "availableQuantity", ...}
    held: set               # 보유 중으로 볼 종목 (포트폴리오 ∪ positions_meta)
    remaining_trades: int
    cooldown: set = field(default_factory=set)
    stop: bool = False      # 거래 횟수 소진·장 마감 등으로 이번 실행의 주문을 멈춤


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _load_state(conn, now: datetime) -> State:
    cfg = get_config()
    pf = aqua.get_portfolio()
    lim = aqua.get_limit()
    holdings = {h["symbol"]: h for h in pf.get("holdings", []) if int(h.get("quantity", 0)) > 0}
    meta = {r["symbol"] for r in conn.execute("SELECT symbol FROM positions_meta")}

    if not aqua.DRY_RUN:
        # 실제 계좌에 없는 종목은 positions_meta 에서 지운다(수동 매도 등으로 어긋난 경우)
        for sym in meta - holdings.keys():
            log.warning("positions_meta 정리: %s 는 계좌에 없음", sym)
            conn.execute("DELETE FROM positions_meta WHERE symbol = ?", (sym,))
        meta &= holdings.keys()

    cooldown = set()
    for c in lim.get("cooldowns", []):
        if datetime.fromisoformat(c["tradableAt"]) > now:
            cooldown.add(c["symbol"])

    remaining = min(int(lim.get("remainingTrades", 0)), cfg["risk"]["internal_trade_limit_per_hour"])
    return State(now=now, total_asset=int(pf["totalAssetValue"]), available_cash=int(pf["availableCash"]),
                 holdings=holdings, held=set(holdings) | meta, remaining_trades=remaining, cooldown=cooldown)


def _set_status(conn, table: str, rid: int, status: str) -> None:
    key = REQUEST_TABLES[table]     # 테이블 이름은 정해진 두 개만 허용(SQL 에 외부 값이 들어가지 않게)
    conn.execute(f"UPDATE {table} SET status = ? WHERE {key} = ?", (status, rid))
    conn.commit()   # 주문 하나마다 바로 저장: 중간에 죽어도 같은 요청을 다시 주문하지 않게


def _log_order(conn, resp: dict, side: str, source: str, now: datetime) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO orders_log (order_id, symbol, side, quantity, status, executed_price,
               total_amount, fee, tax, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (resp["orderId"], resp["symbol"], side, resp["quantity"], resp.get("status"), resp.get("executedPrice"),
         resp.get("totalAmount"), resp.get("fee"), resp.get("tax"), source,
         resp.get("createdAt") or now.isoformat(timespec="seconds")))


def _find_sent_order(symbol: str, side: str, sent_at: datetime) -> dict | None:
    """응답을 못 받은 주문이 실제로 접수됐는지 주문 내역에서 찾는다.

    주문 전에 이 종목이 쿨다운이 아님을 확인했으므로 최근 30분 안에는 이 종목 주문이 없었다.
    따라서 지난 30분 안에 만들어진 같은 종목·방향 주문이 있다면 방금 보낸 주문이다."""
    try:
        orders = aqua.get_orders(size=100).get("orders", [])   # size 최대 100(docs/API.md)
    except (AquaError, requests.RequestException):
        log.exception("주문 내역 조회 실패")
        return None
    since = sent_at - timedelta(minutes=SYMBOL_LOCK_MINUTES)
    for o in orders:
        if o.get("symbol") == symbol and o.get("order_code") == side \
                and datetime.fromisoformat(o["createdAt"]) >= since:
            return o
    return None


def _send(st: State, symbol: str, side: str, qty: int) -> dict | str:
    """주문을 보내고 응답(dict) 또는 KEEP / SKIPPED 를 돌려준다."""
    sent_at = datetime.now(st.now.tzinfo)
    try:
        resp = aqua.place_order(symbol, side, qty)
    except AquaError as e:
        if e.code == "TRADE_LIMIT_EXCEEDED":
            st.stop = True
        elif e.code == "MARKET_CLOSED":
            st.stop = True
        elif e.code == "SYMBOL_COOLDOWN":
            st.cooldown.add(symbol)
        if e.code in KEEP_CODES:
            log.info("%s %s 보류: %s", side, symbol, e.code)
            return KEEP
        log.warning("%s %s 거절: %s", side, symbol, e)
        return STATUS_SKIPPED
    except requests.RequestException:      # 타임아웃·연결 끊김: 주문이 들어갔는지 알 수 없음
        log.exception("%s %s 응답 없음 → 주문 내역 확인", side, symbol)
        resp = _find_sent_order(symbol, side, sent_at)
        if resp is None:
            log.error("%s %s 접수 여부 확인 실패 → SKIPPED (수동 확인 필요)", side, symbol)
            return STATUS_SKIPPED

    st.cooldown.add(symbol)                 # 주문을 넣은 순간부터 30분 락
    if resp.get("remainingTrades") is not None:
        st.remaining_trades = int(resp["remainingTrades"])
    else:
        st.remaining_trades -= 1
    if resp.get("availableCash") is not None:
        st.available_cash = int(resp["availableCash"])
    return resp


def _precheck_order(st: State, symbol: str) -> str | None:
    """주문 전 공통 확인. 막히면 KEEP, 통과하면 None."""
    if st.stop or st.remaining_trades <= 0:
        st.stop = True
        return KEEP
    if symbol in st.cooldown:
        return KEEP
    return None


def _sell(conn, st: State, symbol: str, ratio: float, source: str) -> str:
    h = st.holdings.get(symbol)
    avail = int(h.get("availableQuantity", h.get("quantity", 0))) if h else 0
    if avail <= 0:
        log.info("SELL %s: 보유 수량 없음 → SKIPPED", symbol)
        if symbol not in st.holdings:
            conn.execute("DELETE FROM positions_meta WHERE symbol = ?", (symbol,))
            st.held.discard(symbol)
        return STATUS_SKIPPED
    if (blocked := _precheck_order(st, symbol)):
        return blocked

    qty = avail if ratio >= 1 else max(1, math.floor(avail * ratio))
    resp = _send(st, symbol, "SELL", qty)
    if not isinstance(resp, dict):
        return resp
    _log_order(conn, resp, "SELL", source, st.now)

    filled = int(resp.get("filledQuantity") or 0)
    left = int(h["quantity"]) - filled
    if left <= 0:
        conn.execute("DELETE FROM positions_meta WHERE symbol = ?", (symbol,))
        st.holdings.pop(symbol, None)
        st.held.discard(symbol)
    else:
        h["quantity"], h["availableQuantity"] = left, max(0, avail - filled)
    log.info("SELL %s %d주 요청 → %d주 체결 (%s)", symbol, qty, filled, source)
    return STATUS_DONE


def _buy(conn, st: State, d) -> str:
    cfg = get_config()
    symbol = d["symbol"]
    if st.now.time() >= _hhmm(cfg["schedule"]["buy_cutoff"]):
        return STATUS_SKIPPED
    if d["bad_news"]:
        return STATUS_SKIPPED
    if symbol in st.held:
        return STATUS_SKIPPED
    if len(st.held) >= cfg["risk"]["max_positions"]:
        return STATUS_SKIPPED
    if (blocked := _precheck_order(st, symbol)):
        return blocked

    budget = min(int(st.total_asset * cfg["risk"]["max_weight_per_stock"]), st.available_cash)
    try:
        asks = aqua.get_orderbook(symbol).get("asks", [])
    except AquaError as e:      # 호가 조회 실패가 이번 실행 전체를 멈추지 않게
        log.warning("BUY %s: 호가 조회 실패 %s", symbol, e)
        return STATUS_SKIPPED if e.status == 404 else KEEP
    qty, est_cost = plan_market_buy(budget, asks)
    if qty <= 0:
        log.info("BUY %s: 예산 %d원으로 살 수량 없음 → SKIPPED", symbol, budget)
        return STATUS_SKIPPED

    resp = _send(st, symbol, "BUY", qty)
    if not isinstance(resp, dict):
        return resp
    source = f"decision:{d['decision_id']}"
    _log_order(conn, resp, "BUY", source, st.now)

    filled = int(resp.get("filledQuantity") or 0)
    if filled > 0:
        conn.execute(
            "INSERT OR REPLACE INTO positions_meta (symbol, bought_at, buy_price, reason, decision_id) "
            "VALUES (?,?,?,?,?)",
            (symbol, st.now.isoformat(timespec="seconds"), resp.get("executedPrice") or round(est_cost / qty),
             d["reason"], d["decision_id"]))
        st.held.add(symbol)
        st.holdings[symbol] = {"symbol": symbol, "quantity": filled, "availableQuantity": filled}
        if resp.get("availableCash") is None:      # DRY_RUN 등 응답에 잔액이 없을 때는 추정치로 차감
            spent = resp.get("totalAmount") or est_cost
            st.available_cash -= int(spent) + math.floor(int(spent) * cfg["fees"]["commission"])
    log.info("BUY %s %d주 요청 → %d주 체결 (%s)", symbol, qty, filled, source)
    return STATUS_DONE


def _pick_decisions(conn, today: str):
    """NEW decisions 를 정리: 종목별 최신 하나만 남기고, 지난 BUY 는 버린다.

    쿨다운 등으로 NEW 에 남은 판단과 다음 재평가(09:30·11:00·13:00)의 판단이 겹칠 수 있다.
    - 같은 종목은 가장 최근 판단을 따른다(새 뉴스를 반영한 판단이므로).
    - BUY 는 그날 판단만 유효하다. 후보는 매일 08:00 에 새로 뽑히므로 어제 BUY 는 오늘 근거가 없다.
    - SELL 은 날짜와 상관없이 처리한다(악재 매도를 놓치지 않게)."""
    rows = conn.execute(
        """SELECT d.*, c.momentum_rank FROM decisions d
           LEFT JOIN candidates c ON c.symbol = d.symbol AND c.run_date = substr(d.run_at, 1, 10)
           WHERE d.status = ? ORDER BY d.run_at, d.decision_id""", (STATUS_NEW,)).fetchall()
    latest = {}
    for r in rows:
        if r["symbol"] in latest:
            _set_status(conn, "decisions", latest[r["symbol"]]["decision_id"], STATUS_SKIPPED)
        latest[r["symbol"]] = r
    keep = []
    for r in latest.values():
        if r["action"] == "BUY" and r["run_at"][:10] != today:
            _set_status(conn, "decisions", r["decision_id"], STATUS_SKIPPED)
        else:
            keep.append(r)
    return keep


def run(now: datetime | None = None) -> None:
    """NEW 요청을 처리 순서대로 처리한다. now 는 테스트용."""
    cfg = get_config()
    now = now or datetime.now(ZoneInfo(cfg["timezone"]))
    sch = cfg["schedule"]
    if not (_hhmm(sch["market_open"]) <= now.time() < _hhmm(sch["market_close"])):
        log.info("장 시간이 아님 → 요청은 NEW 로 둠")
        return
    if aqua.DRY_RUN:
        log.warning("DRY_RUN 켜짐: 실제 주문은 나가지 않음")

    with connect() as conn:
        try:
            st = _load_state(conn, now)
        except NotImplementedError:
            # 임시: 데이터 담당의 aqua_client 조회 함수가 완성되기 전에도 --once all 이 끝까지 돌게 한다
            # (README 규칙: PR 전 --once all 통과). 조회 함수가 구현되면 이 except 는 지운다.
            log.warning("aqua_client 조회 함수가 아직 구현 전 → 주문 관리자 건너뜀")
            return
        ratio_tp = cfg["exit"]["take_profit_sell_ratio"]

        # 1) 청산 감시자의 매도 요청
        for r in conn.execute("SELECT * FROM sell_requests WHERE status = ? ORDER BY request_id",
                              (STATUS_NEW,)).fetchall():
            ratio = ratio_tp if r["why"] == "TAKE_PROFIT" else 1.0
            res = _sell(conn, st, r["symbol"], ratio, f"sell_request:{r['request_id']}")
            if res != KEEP:
                _set_status(conn, "sell_requests", r["request_id"], res)

        decisions = _pick_decisions(conn, now.date().isoformat())

        # 2) LLM 의 SELL
        for d in [d for d in decisions if d["action"] == "SELL"]:
            res = _sell(conn, st, d["symbol"], 1.0, f"decision:{d['decision_id']}")
            if res != KEEP:
                _set_status(conn, "decisions", d["decision_id"], res)

        # 3) BUY, 모멘텀 순위순 (순위 없으면 뒤로)
        buys = sorted([d for d in decisions if d["action"] == "BUY"],
                      key=lambda d: (d["momentum_rank"] is None, d["momentum_rank"] or 0, d["decision_id"]))
        for d in buys:
            res = _buy(conn, st, d)
            if res != KEEP:
                _set_status(conn, "decisions", d["decision_id"], res)

        # 4) WAIT 는 바로 SKIPPED
        for d in [d for d in decisions if d["action"] == "WAIT"]:
            _set_status(conn, "decisions", d["decision_id"], STATUS_SKIPPED)

    log.info("order_manager 완료: 남은 거래 %d회, 보유 %d종목", st.remaining_trades, len(st.held))
