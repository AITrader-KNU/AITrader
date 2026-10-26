"""4-5 청산 감시자 — 장중 exit_check_minutes(15분)마다 실행. LLM 없이 숫자 규칙만 사용.

규칙(config.yaml exit.*):  손절 stop_loss(-0.04) → 익절 take_profit(0.06) → 보유기간 max_holding_days(5거래일)
우선순위: STOP_LOSS > TAKE_PROFIT > EXPIRED
악재 매도는 여기서 다루지 않음(판단 에이전트가 decisions 에 SELL).

읽는 곳: AQuA get_portfolio(보유·평균단가·현재가), get_limit(30분 락), positions_meta(bought_at)
쓰는 곳: sell_requests (created_at, symbol, why, status='NEW') — 수량 열이 없으므로 '전량 매도' 요청

쿨다운(30분 락) 처리: 락이 걸린 종목은 신호를 내지 않고 보류. 상태를 저장하지 않고 매번 다시 계산하므로,
락이 풀린 뒤 첫 점검에서 조건이 여전히 맞으면 자동으로 요청이 나간다.
중복 방지: 같은 종목에 status='NEW' 요청이 이미 있으면 새로 쓰지 않는다.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from common.config import get_config
from common.db import STATUS_NEW, connect, init_db
from common.logger import get_logger

log = get_logger("exit_monitor")

EPS = 1e-10  # 부동소수점 경계 오차 방지


def _tz(cfg):
    return ZoneInfo(cfg["timezone"])


def now_kst(cfg=None) -> datetime:
    return datetime.now(_tz(cfg or get_config()))


def parse_time(s: str, tz) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def in_market(now: datetime, cfg: dict) -> bool:
    s = cfg["schedule"]
    if now.weekday() >= 5 or now.date().isoformat() in s.get("holidays", []):
        return False
    hm = now.strftime("%H:%M")
    return s["market_open"] <= hm < s["market_close"]


def holding_days(bought_at: str, now: datetime, cfg: dict) -> int:
    """매수일부터 오늘까지 지난 거래일 수(주말·휴장일 제외). 같은 날이면 0."""
    start = parse_time(bought_at, _tz(cfg)).date()
    return int(np.busday_count(start, now.date(), holidays=cfg["schedule"].get("holidays", [])))


def signal(h: dict, bought_at: str | None, now: datetime, cfg: dict) -> tuple[str, str] | None:
    """한 종목의 청산 신호 (why, 설명) 또는 None."""
    ex = cfg["exit"]
    avg, cur = h["avgBuyPrice"], h["currentPrice"]
    if not avg or avg <= 0 or not cur or cur <= 0:
        log.warning("%s 가격 정보 이상(avg=%s, cur=%s) — 건너뜀", h["symbol"], avg, cur)
        return None
    pnl = round(cur / avg - 1, 10)
    if pnl <= -abs(ex["stop_loss"]) + EPS:
        return "STOP_LOSS", f"수익률 {pnl:+.2%} <= 손절 {-abs(ex['stop_loss']):+.0%}"
    if pnl >= abs(ex["take_profit"]) - EPS:
        return "TAKE_PROFIT", f"수익률 {pnl:+.2%} >= 익절 {abs(ex['take_profit']):+.0%}"
    if bought_at:
        d = holding_days(bought_at, now, cfg)
        if d >= ex["max_holding_days"]:
            return "EXPIRED", f"보유 {d}거래일 >= {ex['max_holding_days']}"
    return None


def evaluate(holdings: list[dict], meta: dict[str, str], cooldowns: list[dict],
             now: datetime, cfg: dict) -> list[dict]:
    """순수 함수. 반환: [{symbol, why, detail}]  (쿨다운 중이거나 매도 가능 수량 0인 종목은 제외)."""
    tz = _tz(cfg)
    locked = {c["symbol"] for c in cooldowns if parse_time(c["tradableAt"], tz) > now}
    out = []
    for h in holdings:
        sym = h["symbol"]
        if h.get("quantity", 0) <= 0:
            continue
        if sym not in meta:
            log.warning("%s: positions_meta 에 매수 시각이 없어 보유기간 규칙은 건너뜁니다", sym)
        sig = signal(h, meta.get(sym), now, cfg)
        if sig is None:
            continue
        if sym in locked:
            log.info("%s: %s 신호지만 30분 락 중 → 보류 (%s)", sym, sig[0], sig[1])
            continue
        if h.get("availableQuantity", h["quantity"]) <= 0:
            log.info("%s: %s 신호지만 매도 가능 수량 0(대기 주문 있음) → 보류", sym, sig[0])
            continue
        out.append({"symbol": sym, "why": sig[0], "detail": sig[1]})
    return out


def run_once(now: datetime | None = None, portfolio: dict | None = None,
             limit: dict | None = None) -> list[dict]:
    """1회 점검. portfolio/limit 을 주면 API 를 부르지 않음(테스트용). 새로 쓴 요청 목록 반환."""
    init_db()
    cfg = get_config()
    now = now or now_kst(cfg)
    if portfolio is None or limit is None:
        from common import aqua_client
        portfolio = portfolio if portfolio is not None else aqua_client.get_portfolio()
        limit = limit if limit is not None else aqua_client.get_limit()

    written = []
    with connect() as conn:
        meta = {r["symbol"]: r["bought_at"] for r in conn.execute("SELECT symbol, bought_at FROM positions_meta")}
        pending = {r["symbol"] for r in conn.execute("SELECT symbol FROM sell_requests WHERE status=?", (STATUS_NEW,))}
        for s in evaluate(portfolio.get("holdings", []), meta, limit.get("cooldowns", []), now, cfg):
            if s["symbol"] in pending:
                log.info("%s: 이미 처리 대기 중인 매도 요청이 있어 건너뜀", s["symbol"])
                continue
            conn.execute(
                "INSERT INTO sell_requests (created_at, symbol, why, status) VALUES (?,?,?,?)",
                (now.isoformat(timespec="seconds"), s["symbol"], s["why"], STATUS_NEW),
            )
            log.info("매도 요청: %s %s (%s)", s["symbol"], s["why"], s["detail"])
            written.append(s)
    return written


def run() -> list[dict]:
    """main.py 스케줄러가 exit_check_minutes 마다 부르는 진입점. 장이 닫혀 있으면 아무것도 하지 않는다."""
    cfg = get_config()
    now = now_kst(cfg)
    if not in_market(now, cfg):
        log.info("장 시간이 아니라 청산 점검을 건너뜁니다 (%s)", now.isoformat(timespec="seconds"))
        return []
    try:
        return run_once(now)
    except Exception:
        log.exception("청산 점검 실패 — 다음 주기에 다시 시도")
        return []


def main() -> None:
    """단독 실행용 루프(스케줄러 없이 테스트할 때). 운영에서는 main.py 가 run() 을 부른다."""
    cfg = get_config()
    step = cfg["schedule"]["exit_check_minutes"] * 60
    while True:
        now = now_kst(cfg)
        if in_market(now, cfg):
            try:
                run_once(now)
            except Exception:
                log.exception("청산 점검 실패 — 다음 주기에 다시 시도")
        time.sleep(step)


if __name__ == "__main__":
    main()
