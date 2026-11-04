"""판단 시점 가격 복원 — decision_prices 기록을 시작하기 전 판단의 가격을 AQuA 1분봉으로 채운다(조회만, 주문 없음).

  python -m tools.backfill_decision_prices --dry-run   # 쓰지 않고 채울 값만 출력
  python -m tools.backfill_decision_prices             # decision_prices 에 저장

가격 = 판단 시각(run_at) 이전에 끝난 마지막 1분봉의 종가.
  1분봉 timestamp 는 그 봉이 시작한 분이므로, timestamp + 1분 <= run_at 인 봉 중 가장 늦은 것을 쓴다.
  실시간 기록(order_manager/decision_prices.py)은 판단 직후 현재가라서 몇 초~몇 분 차이가 날 수 있다.
price_time 에는 쓴 1분봉의 timestamp 를, recorded_at 에는 'backfill:' + 실행 시각을 남겨 실시간 기록과 구분한다.

AQuA 1분봉은 11/2 08:38 분부터 남아 있음을 11/4 VM 에서 확인했다(보관 기간은 API 문서에 없음).
1분봉은 종목당 200개씩 nextBefore 로 이어받는다(docs/API.md). 그 종목의 가장 이른 판단 시각보다
과거까지 받으면 멈춘다. 운영 중인 서비스와 호출 한도(분당 100회)를 나눠 쓰므로 장 마감 후에 실행한다.
"""
import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common import aqua_client
from common.config import get_config
from common.db import connect, init_db

ONE_MINUTE = timedelta(minutes=1)
MAX_PAGES = 30   # 종목당 최대 30번 x 200개 = 6000분(장중 하루 약 380분 기준 2주 남짓). 무한 반복 방지


def missing_decisions(conn) -> list[dict]:
    """decision_prices 에 가격이 없는 판단 전부."""
    return [dict(r) for r in conn.execute(
        """SELECT d.decision_id, d.symbol, d.run_at FROM decisions d
           LEFT JOIN decision_prices p ON p.decision_id = d.decision_id
           WHERE p.decision_id IS NULL ORDER BY d.run_at""")]


def load_minutes(symbol: str, earliest: datetime) -> list[tuple[datetime, dict]]:
    """earliest 보다 과거까지 1분봉을 이어받아 (시작 시각, 봉) 을 시각 순으로 돌려준다."""
    bars, before = {}, None
    for _ in range(MAX_PAGES):
        data = aqua_client.get_candles(symbol, "1m", 200, before)
        candles = data.get("candles", [])
        if not candles:
            break
        for c in candles:
            bars[c["timestamp"]] = c
        oldest = min(datetime.fromisoformat(c["timestamp"]) for c in candles)
        before = data.get("nextBefore")
        if oldest + ONE_MINUTE <= earliest or not before:
            break
    return sorted((datetime.fromisoformat(ts), c) for ts, c in bars.items())


def price_at(bars: list[tuple[datetime, dict]], run_at: datetime) -> tuple[int, str] | None:
    """run_at 이전에 끝난 마지막 1분봉의 (종가, timestamp). 그런 봉이 없으면 None."""
    done = [(t, c) for t, c in bars if t + ONE_MINUTE <= run_at]
    if not done:
        return None
    t, c = done[-1]
    return int(c["closePrice"]), c["timestamp"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="저장하지 않고 채울 값만 출력")
    args = p.parse_args()
    init_db()
    stamp = "backfill:" + datetime.now(ZoneInfo(get_config()["timezone"])).isoformat(timespec="seconds")

    with connect() as conn:
        todo = missing_decisions(conn)
    by_symbol: dict[str, list[dict]] = {}
    for d in todo:
        by_symbol.setdefault(d["symbol"], []).append(d)
    print(f"가격이 없는 판단 {len(todo)}건, {len(by_symbol)}종목")

    filled, skipped = [], []
    for symbol, ds in by_symbol.items():
        run_ats = [datetime.fromisoformat(d["run_at"]) for d in ds]
        try:
            bars = load_minutes(symbol, min(run_ats))
        except aqua_client.AquaError as e:
            print(f"  {symbol}: 1분봉 조회 실패 ({e.code}) — 건너뜀")
            skipped += [d["decision_id"] for d in ds]
            continue
        for d, run_at in zip(ds, run_ats):
            got = price_at(bars, run_at)
            if got is None:
                skipped.append(d["decision_id"])
                continue
            filled.append((d["decision_id"], got[0], got[1], stamp))
            print(f"  #{d['decision_id']} {d['run_at'][:16]} {symbol} → {got[0]:,}원 ({got[1][11:16]} 봉)")

    if not args.dry_run and filled:
        with connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO decision_prices (decision_id, price, price_time, recorded_at) "
                             "VALUES (?,?,?,?)", filled)
    print(f"{'(dry-run) ' if args.dry_run else ''}채움 {len(filled)}건, 못 채움 {len(skipped)}건"
          + (f": {skipped}" if skipped else ""))


if __name__ == "__main__":
    main()
