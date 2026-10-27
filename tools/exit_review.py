"""매주 금요일 청산 규칙 점검 — 손절·익절 값을 바꿀 조건을 만족했는지 확인한다(조회만, 주문 없음).

  python -m tools.exit_review                    # 운영 시작부터 지금까지 전체
  python -m tools.exit_review --since 2026-11-02 # 이 날짜 이후 매도만

조정 조건(config.yaml review 섹션, 결과를 보기 전에 정해 둔 값)
  1. 손절 후 회복률: 손절 종목 중 recovery_ratio 이상이 매도 후 window_days 거래일 안에
     종가 기준으로 매수가 이상 회복 → 손절이 너무 좁다(손절 -4% → -7%, 익절 +6% → +10% 검토)
  2. 손절 체결 수준: 손절 거래의 평균 손익(비용 제외)이 stop_fill_limit 보다 나쁨 → 손절 폭·점검 주기 재검토
  3. 익절 후 추가 상승: 익절 종목 중 tp_extra_ratio 이상이 window_days 안에 종가가 매수가 대비 tp_extra 를 넘음
     → 익절이 너무 이르다(익절 +6% → +10% 검토)
  해당 매도가 min_cases 건 미만이면 판단 보류.

읽는 것: orders_log(매수·매도 체결가), sell_requests(매도 이유), daily_candles(매도 후 종가)
매도 후 window_days 거래일의 일봉이 아직 다 쌓이지 않은 거래는 회복·추가 상승 판단에서 뺀다.
"""
import argparse

from common.config import get_config
from common.db import connect, init_db


def load_exits(conn, since: str | None) -> list[dict]:
    """청산 감시자가 낸 매도(손절·익절·기간 만료)마다 직전 매수가를 짝지어 돌려준다."""
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
            continue                                   # 매수 기록이 없는 보유분(수동 매수 등)은 판단에서 뺌
        exits.append(dict(symbol=s["symbol"], why=s["why"], sell_date=s["created_at"][:10],
                          buy=buy["executed_price"], sell=s["sell_price"],
                          ret=s["sell_price"] / buy["executed_price"] - 1))
    return exits


def closes_after(conn, symbol: str, day: str, n: int) -> list[float]:
    """매도일 다음 거래일부터 n개 종가. 아직 n개가 안 쌓였으면 있는 만큼."""
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
        e["ready"] = len(after) >= n                     # 매도 후 n거래일이 다 지났는지
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
    init_db()                                          # 처음 실행해 테이블이 없을 때를 대비
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
