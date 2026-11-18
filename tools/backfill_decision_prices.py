import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common import aqua_client
from common.config import get_config
from common.db import connect, init_db

ONE_MINUTE = timedelta(minutes=1)
MAX_PAGES = 30


def missing_decisions(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT d.decision_id, d.symbol, d.run_at FROM decisions d
           LEFT JOIN decision_prices p ON p.decision_id = d.decision_id
           WHERE p.decision_id IS NULL ORDER BY d.run_at""")]


def load_minutes(symbol: str, earliest: datetime) -> list[tuple[datetime, dict]]:
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
