import argparse

from common.config import get_config
from common.db import connect, init_db


def load_exits(conn, since: str | None) -> list[dict]:
    sells = conn.execute(
        """SELECT o.symbol, o.executed_price AS sell_price, o.created_at, s.why
           FROM orders_log o JOIN sell_requests s ON o.source = 'sell_request:' || s.request_id
           WHERE o.side = 'SELL' AND o.executed_price IS NOT NULL AND (? IS NULL OR o.created_at >= ?)
           ORDER BY o.created_at""", (since, since)).fetchall()
    exits = []
    for s in sells:
        buy = conn.execute(
            """SELECT executed_price FROM orders_log
               WHERE symbol = ? AND side = 'BUY' AND executed_price IS NOT NULL AND created_at < ?
               ORDER BY created_at DESC LIMIT 1""", (s["symbol"], s["created_at"])).fetchone()
        if buy is None:
            continue
        exits.append(dict(symbol=s["symbol"], why=s["why"], sell_date=s["created_at"][:10],
                          buy=buy["executed_price"], sell=s["sell_price"],
                          ret=s["sell_price"] / buy["executed_price"] - 1))
    return exits


def closes_after(conn, symbol: str, day: str, n: int) -> list[float]:
    rows = conn.execute("SELECT close FROM daily_candles WHERE symbol = ? AND date > ? ORDER BY date LIMIT ?",
                        (symbol, day, n)).fetchall()
    return [r["close"] for r in rows]


def verdict(hit: int, total: int, ratio: float, min_cases: int) -> str:
    if total < min_cases:
        return f"판단 보류 ({total}건 < 최소 {min_cases}건)"
    return f"{'조정 조건 충족' if hit / total >= ratio else '미충족'} ({hit}/{total}건, 기준 {ratio:.0%})"


def review(conn, since: str | None, cfg: dict) -> dict:
    rv = cfg["review"]
    n = rv["window_days"]
    exits = load_exits(conn, since)
    for e in exits:
        after = closes_after(conn, e["symbol"], e["sell_date"], n)
        e["ready"] = len(after) >= n
        e["max_close_ret"] = max(after) / e["buy"] - 1 if after else None

    stops = [e for e in exits if e["why"] == "STOP_LOSS"]
    takes = [e for e in exits if e["why"] == "TAKE_PROFIT"]
    stops_ready = [e for e in stops if e["ready"]]
    takes_ready = [e for e in takes if e["ready"]]
    recovered = sum(e["max_close_ret"] >= 0 for e in stops_ready)
    extra = sum(e["max_close_ret"] >= rv["tp_extra"] for e in takes_ready)
    avg_stop = sum(e["ret"] for e in stops) / len(stops) if stops else None

    if len(stops) < rv["min_cases"]:
        fill = f"판단 보류 ({len(stops)}건 < 최소 {rv['min_cases']}건)"
    else:
        bad = avg_stop < rv["stop_fill_limit"]
        fill = f"{'조정 조건 충족' if bad else '미충족'} (평균 {avg_stop:+.2%}, 기준 {rv['stop_fill_limit']:+.0%})"
    return dict(
        exits=exits,
        recovery=verdict(recovered, len(stops_ready), rv["recovery_ratio"], rv["min_cases"]),
        fill=fill,
        take_extra=verdict(extra, len(takes_ready), rv["tp_extra_ratio"], rv["min_cases"]),
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--since", help="이 날짜(YYYY-MM-DD) 이후 매도만 점검")
    args = p.parse_args()
    cfg = get_config()
    init_db()
    with connect() as conn:
        r = review(conn, args.since, cfg)

    ex = cfg["exit"]
    print(f"지금 값: 손절 {ex['stop_loss']:+.0%}, 익절 {ex['take_profit']:+.0%}, 최대 보유 {ex['max_holding_days']}거래일")
    print(f"\n[청산 감시자 매도 {len(r['exits'])}건]")
    for e in r["exits"]:
        after = "대기" if not e["ready"] else f"{e['max_close_ret']:+.1%}"
        print(f"  {e['sell_date']} {e['symbol']} {e['why']:<11} 매수 {e['buy']:>9,} → 매도 {e['sell']:>9,} "
              f"({e['ret']:+.1%})  매도 후 {cfg['review']['window_days']}일 최고 종가(매수가 대비) {after}")
    print("\n[조정 조건]")
    print(f"  1. 손절 후 회복률   : {r['recovery']}")
    print(f"  2. 손절 체결 수준   : {r['fill']}")
    print(f"  3. 익절 후 추가 상승: {r['take_extra']}")
    print("\n조건을 충족하면 전략 담당과 확인 후, 장 마감 뒤 config.yaml 의 exit 값을 바꿔 반영합니다.")


if __name__ == "__main__":
    main()
