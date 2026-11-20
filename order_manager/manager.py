import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from common import aqua_client as aqua
from common.aqua_client import AquaError
from common.config import get_config
from common.db import STATUS_DONE, STATUS_NEW, STATUS_SKIPPED, connect
from common.logger import get_logger

log = get_logger("order_manager")

KEEP = "KEEP"
KEEP_CODES = {"SYMBOL_COOLDOWN", "TRADE_LIMIT_EXCEEDED", "MARKET_CLOSED", "RATE_LIMIT_EXCEEDED", "UNAUTHORIZED"}
NETWORK_ERROR = "NETWORK_ERROR"
SYMBOL_LOCK_MINUTES = 30
REQUEST_TABLES = {"decisions": "decision_id", "sell_requests": "request_id"}


def plan_market_buy(budget: int, asks: list[dict], commission: float | None = None) -> tuple[int, int]:
    if commission is None:
        commission = get_config()["fees"]["commission"]
    max_cost = math.floor(budget / (1 + commission))
    qty, cost = 0, 0
    for level in asks:
        price, volume = int(level["price"]), int(level["volume"])
        if price <= 0 or volume <= 0:
            continue
        k = min(volume, (max_cost - cost) // price)
        if k <= 0:
            break
        qty, cost = qty + k, cost + k * price
        if k < volume:
            break
    return qty, cost


@dataclass
class State:
    now: datetime
    total_asset: int
    available_cash: int
    holdings: dict
    held: set
    remaining_trades: int
    cooldown: set = field(default_factory=set)
    stop: bool = False
    equity_cap: float | None = None


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
                 holdings=holdings, held=set(holdings) | meta, remaining_trades=remaining, cooldown=cooldown,
                 equity_cap=_equity_cap(conn, now, cfg))


def _equity_cap(conn, now: datetime, cfg: dict) -> float | None:
    r = cfg.get("regime") or {}
    if not r.get("enabled", False):
        return None
    row = conn.execute("SELECT regime, max_equity_ratio FROM market_regime WHERE run_date = ?",
                       (now.date().isoformat(),)).fetchone()
    if row is None:
        cap = float(r["max_equity_ratio"][r["default_when_missing"]])
        log.warning("오늘 시장 국면 기록 없음 → %s 상한 %.0f%% 사용", r["default_when_missing"], cap * 100)
        return cap
    log.info("오늘 시장 국면 %s → 주식 비중 상한 %.0f%%", row["regime"], row["max_equity_ratio"] * 100)
    return float(row["max_equity_ratio"])


def _blocking_disclosure(conn, symbol: str, now: datetime, cfg: dict) -> str | None:
    dc = cfg.get("disclosure") or {}
    keywords = dc.get("block_keywords") or []
    if not dc.get("enabled", False) or not keywords:
        return None
    since = (now.date() - timedelta(days=dc["block_buy_days"] - 1)).isoformat()
    for r in conn.execute("SELECT report_nm FROM disclosures WHERE symbol = ? AND rcept_dt >= ? ORDER BY rcept_dt DESC",
                          (symbol, since)):
        if any(k in r["report_nm"] for k in keywords):
            return r["report_nm"]
    return None

def _reentry_block(conn, symbol: str, now: datetime, cfg: dict) -> str | None:
    ex = cfg["exit"]
    days, reasons = ex.get("reentry_block_days", 0), ex.get("reentry_block_reasons") or []
    if days <= 0 or not reasons:
        return None
    r = conn.execute("SELECT why, created_at FROM sell_requests WHERE symbol = ? AND status = ? "
                     "ORDER BY request_id DESC LIMIT 1", (symbol, STATUS_DONE)).fetchone()
    if r is None or r["why"] not in reasons:
        return None
    sold = datetime.fromisoformat(r["created_at"]).date()
    elapsed = np.busday_count(sold, now.date(), holidays=cfg["schedule"].get("holidays") or [])
    if elapsed < days:
        return f"{sold} {r['why']}"
    return None


def _set_status(conn, table: str, rid: int, status: str) -> None:
    key = REQUEST_TABLES[table]
    conn.execute(f"UPDATE {table} SET status = ? WHERE {key} = ?", (status, rid))
    conn.commit()


def _log_order(conn, resp: dict, side: str, source: str, now: datetime) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO orders_log (order_id, symbol, side, quantity, status, executed_price,
               total_amount, fee, tax, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (resp["orderId"], resp["symbol"], side, resp["quantity"], resp.get("status"), resp.get("executedPrice"),
         resp.get("totalAmount"), resp.get("fee"), resp.get("tax"), source,
         resp.get("createdAt") or now.isoformat(timespec="seconds")))


def _find_sent_order(symbol: str, side: str, sent_at: datetime) -> dict | None:
    try:
        orders = aqua.get_orders(size=100).get("orders", [])
    except AquaError:
        log.exception("주문 내역 조회 실패")
        return None
    since = sent_at - timedelta(minutes=SYMBOL_LOCK_MINUTES)
    for o in orders:
        if o.get("symbol") == symbol and o.get("order_code") == side \
                and datetime.fromisoformat(o["createdAt"]) >= since:
            return o
    return None


def _send(st: State, symbol: str, side: str, qty: int) -> dict | str:
    sent_at = datetime.now(st.now.tzinfo)
    try:
        resp = aqua.place_order(symbol, side, qty)
    except AquaError as e:
        if e.code == NETWORK_ERROR:
            log.warning("%s %s 응답 없음 → 주문 내역 확인", side, symbol)
            resp = _find_sent_order(symbol, side, sent_at)
            if resp is None:
                log.error("%s %s 접수 여부 확인 실패 → SKIPPED (수동 확인 필요)", side, symbol)
                return STATUS_SKIPPED
        else:
            if e.code in ("TRADE_LIMIT_EXCEEDED", "MARKET_CLOSED"):
                st.stop = True
            elif e.code == "SYMBOL_COOLDOWN":
                st.cooldown.add(symbol)
            if e.code in KEEP_CODES:
                log.info("%s %s 보류: %s", side, symbol, e.code)
                return KEEP
            log.warning("%s %s 거절: %s", side, symbol, e)
            return STATUS_SKIPPED

    st.cooldown.add(symbol)
    if resp.get("remainingTrades") is not None:
        st.remaining_trades = int(resp["remainingTrades"])
    else:
        st.remaining_trades -= 1
    if resp.get("availableCash") is not None:
        st.available_cash = int(resp["availableCash"])
    return resp


def _precheck_order(st: State, symbol: str) -> str | None:
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
    if (title := _blocking_disclosure(conn, symbol, st.now, cfg)):
        log.info("BUY %s: 차단 공시 '%s' → SKIPPED", symbol, title)
        return STATUS_SKIPPED
    if symbol in st.held:
        return STATUS_SKIPPED
    if (sold := _reentry_block(conn, symbol, st.now, cfg)):
        log.info("BUY %s: %s 매도 후 %d거래일 재매수 금지 → SKIPPED", symbol, sold, cfg["exit"]["reentry_block_days"])
        return STATUS_SKIPPED
    if len(st.held) >= cfg["risk"]["max_positions"]:
        return STATUS_SKIPPED
    if (blocked := _precheck_order(st, symbol)):
        return blocked

    full = int(st.total_asset * cfg["risk"]["max_weight_per_stock"])
    budget = min(full, st.available_cash)
    if st.equity_cap is not None:
        room = int(st.total_asset * st.equity_cap) - (st.total_asset - st.available_cash)
        if room < budget:
            log.info("BUY %s: 시장 국면 상한 %.0f%% → 예산 %d원에서 %d원으로", symbol, st.equity_cap * 100, budget, max(room, 0))
            budget = room
    min_budget = int(full * cfg["risk"]["min_buy_ratio"])
    if budget < min_budget:
        log.info("BUY %s: 예산 %d원 < 최소 %d원(정상 예산의 %.0f%%) → 자투리 매수 안 함, SKIPPED",
                 symbol, max(budget, 0), min_budget, cfg["risk"]["min_buy_ratio"] * 100)
        return STATUS_SKIPPED
    try:
        asks = aqua.get_orderbook(symbol).get("asks", [])
    except AquaError as e:
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
        if resp.get("availableCash") is None:
            spent = resp.get("totalAmount") or est_cost
            st.available_cash -= int(spent) + math.floor(int(spent) * cfg["fees"]["commission"])
    log.info("BUY %s %d주 요청 → %d주 체결 (%s)", symbol, qty, filled, source)
    return STATUS_DONE


def _pick_decisions(conn, today: str):
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
    cfg = get_config()
    now = now or datetime.now(ZoneInfo(cfg["timezone"]))
    sch = cfg["schedule"]
    if not (_hhmm(sch["market_open"]) <= now.time() < _hhmm(sch["market_close"])):
        log.info("장 시간이 아님 → 요청은 NEW 로 둠")
        return
    if aqua.DRY_RUN:
        log.warning("DRY_RUN 켜짐: 실제 주문은 나가지 않음")

    with connect() as conn:
        st = _load_state(conn, now)
        ratio_tp = cfg["exit"]["take_profit_sell_ratio"]

        for r in conn.execute("SELECT * FROM sell_requests WHERE status = ? ORDER BY request_id",
                              (STATUS_NEW,)).fetchall():
            ratio = ratio_tp if r["why"] == "TAKE_PROFIT" else 1.0
            res = _sell(conn, st, r["symbol"], ratio, f"sell_request:{r['request_id']}")
            if res != KEEP:
                _set_status(conn, "sell_requests", r["request_id"], res)

        decisions = _pick_decisions(conn, now.date().isoformat())

        for d in [d for d in decisions if d["action"] == "SELL"]:
            res = _sell(conn, st, d["symbol"], 1.0, f"decision:{d['decision_id']}")
            if res != KEEP:
                _set_status(conn, "decisions", d["decision_id"], res)

        buys = sorted([d for d in decisions if d["action"] == "BUY"],
                      key=lambda d: (d["momentum_rank"] is None, d["momentum_rank"] or 0, d["decision_id"]))
        for d in buys:
            res = _buy(conn, st, d)
            if res != KEEP:
                _set_status(conn, "decisions", d["decision_id"], res)

        for d in [d for d in decisions if d["action"] == "WAIT"]:
            _set_status(conn, "decisions", d["decision_id"], STATUS_SKIPPED)

    log.info("order_manager 완료: 남은 거래 %d회, 보유 %d종목", st.remaining_trades, len(st.held))
