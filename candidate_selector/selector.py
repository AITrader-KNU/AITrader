"""4-2 후보 선정기 — 매일 장 시작 전(premarket) 실행.

흐름: 유동성 상위 N → 추세(종가 > 20일선) → 모멘텀 점수 → 과열 제외 → 상위 top_n 을 candidates 에 저장.
모든 수치는 config.yaml 에서만 읽는다(코드에 숫자 직접 쓰지 않기).

읽는 곳: daily_candles (run_date 이전 날짜의 봉만 사용 → 당일 미완성 봉 제외)
쓰는 곳: candidates (run_date, symbol, name, momentum_rank, ret_5d, above_ma20)

점수 = w_ret × rank(ret_120d, 최근 skip일 제외) + w_high × rank(종가 / 52주 고가)
  ret_120d = close[t-skip] / close[t-skip-lookback] - 1   (t = 마지막 봉)
  rank = 풀 안에서의 백분위(0~1, 클수록 좋음)

config 에 없으면 아래 기본값을 씁니다(추가를 권장):
  universe.max_daily_move        (0.30)  하루 ±30% 넘는 봉이 있는 종목은 데이터 오류/분할 의심 → 제외
  candidates.rank_among          ("all") 순위를 매기는 풀: "all"=유동성 상위 전체, "trend"=추세 통과 종목끼리
  candidates.momentum.weight_return / weight_high   (0.5 / 0.5)
  candidates.ma_slope_days       (5)     require_rising_ma 가 true 일 때 20일선 비교 간격
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from common.config import get_config
from common.db import connect, init_db
from common.logger import get_logger

log = get_logger("selector")

STALE_DAYS = 4  # 마지막 봉이 run_date 보다 이만큼 이상 오래됐으면 경고(연휴 감안)


def _names() -> dict[str, str]:
    """종목명 {symbol: name}. API 가 아직 구현 전이거나 실패하면 빈 dict (name 은 NULL 로 저장)."""
    try:
        from common import aqua_client

        return {s["symbol"]: s["name"] for s in aqua_client.list_stocks()}
    except Exception as e:  # NotImplementedError 포함 — 이름은 부가 정보라서 선정을 막지 않는다
        log.warning("종목명 조회 실패, name 은 비워 둡니다: %r", e)
        return {}


def _load_candles(conn, run_date: str, max_bars: int) -> pd.DataFrame:
    """run_date 이전 봉만. 종목당 최근 max_bars 개."""
    df = pd.read_sql_query(
        "SELECT symbol, date, high, close, volume FROM daily_candles "
        "WHERE date < ? ORDER BY symbol, date",
        conn,
        params=(run_date,),
    )
    if df.empty:
        return df
    for c in ("high", "close", "volume"):
        df[c] = df[c].astype("float64")  # int 곱셈 오버플로 방지
    return df.groupby("symbol", group_keys=False).tail(max_bars)


def compute(df: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, dict]:
    """순수 계산 함수(DB 없음). df: symbol,date,high,close,volume. 반환: (선정 결과, 단계별 종목 수)."""
    c = cfg["candidates"]
    mom, ov = c["momentum"], c["overheat"]
    lb, skip, hiw = mom["lookback_days"], mom["skip_recent_days"], mom["high_window_days"]
    maw, liq_w = c["ma_window"], cfg["universe"]["liquidity_window"]
    liq_n, top_n = cfg["universe"]["liquidity_top_n"], c["top_n"]
    max_move = cfg["universe"].get("max_daily_move", 0.30)
    rank_among = c.get("rank_among", "all")
    w_ret, w_high = mom.get("weight_return", 0.5), mom.get("weight_high", 0.5)
    slope = c.get("ma_slope_days", 5)

    need = max(lb + skip + 1, hiw, liq_w, maw + (slope if c.get("require_rising_ma") else 0), 6)
    funnel = {"symbols": int(df["symbol"].nunique()) if len(df) else 0}

    rows = []
    for sym, g in df.groupby("symbol"):
        if len(g) < need:
            continue
        cl, hi, vol = g["close"].to_numpy(), g["high"].to_numpy(), g["volume"].to_numpy()
        cl, hi, vol = cl[-need:], hi[-need:], vol[-need:]
        if (cl <= 0).any() or pd.isna(cl).any() or pd.isna(vol).any() or pd.isna(hi).any():
            continue
        if (abs(cl[1:] / cl[:-1] - 1) > max_move).any():
            continue
        ma = cl[-maw:].mean()
        rising = True
        if c.get("require_rising_ma"):
            rising = ma > cl[-maw - slope : -slope].mean()
        rows.append(
            dict(
                symbol=sym,
                close=cl[-1],
                avg_value=(cl[-liq_w:] * vol[-liq_w:]).mean(),
                above_ma=bool(cl[-1] > ma and rising),
                ret_mom=cl[-1 - skip] / cl[-1 - skip - lb] - 1,
                high_ratio=cl[-1] / hi[-hiw:].max(),
                ret_1d=cl[-1] / cl[-2] - 1,
                ret_5d=cl[-1] / cl[-6] - 1,
            )
        )
    d = pd.DataFrame(rows)
    funnel["history_ok"] = len(d)
    empty = pd.DataFrame(columns=["symbol", "ret_5d", "above_ma20", "score"])
    if d.empty:
        return empty, {**funnel, "liquidity": 0, "trend": 0, "not_overheated": 0, "selected": 0}

    d = d.nlargest(liq_n, "avg_value").copy()
    funnel["liquidity"] = len(d)

    pool = d[d["above_ma"]] if rank_among == "trend" else d
    d["score"] = (
        w_ret * pool["ret_mom"].rank(pct=True) + w_high * pool["high_ratio"].rank(pct=True)
    ).reindex(d.index)
    d = d[d["above_ma"]]
    funnel["trend"] = len(d)

    d = d[(d["ret_5d"] <= ov["max_ret_5d"]) & (d["ret_1d"] <= ov["max_ret_1d"])]
    funnel["not_overheated"] = len(d)

    d = d.sort_values(["score", "avg_value"], ascending=False).head(top_n).copy()
    funnel["selected"] = len(d)
    d["above_ma20"] = 1
    return d, funnel


def run(run_date: date | str | None = None) -> int:
    """후보 선정 후 candidates 에 저장. 같은 run_date 를 다시 돌리면 그날 행을 지우고 새로 씀. 저장 건수 반환."""
    init_db()  # 테이블이 없으면 만듦(이미 있으면 그대로)
    cfg = get_config()
    rd = (run_date or date.today()).isoformat() if not isinstance(run_date, str) else run_date
    c = cfg["candidates"]["momentum"]
    max_bars = max(c["high_window_days"], c["lookback_days"] + c["skip_recent_days"] + 1) + 10

    with connect() as conn:
        df = _load_candles(conn, rd, max_bars)
        if df.empty:
            log.error("[%s] daily_candles 에 봉이 없습니다 — 기존 candidates 는 건드리지 않음", rd)
            return 0
        last = df["date"].max()
        if (date.fromisoformat(rd) - date.fromisoformat(last)).days > STALE_DAYS:
            log.warning("[%s] 최신 봉이 %s 입니다 — 데이터 갱신이 안 됐을 수 있음", rd, last)

        out, funnel = compute(df, cfg)
        log.info("[%s] 단계별 종목 수: %s", rd, funnel)
        if out.empty:
            log.warning("[%s] 선정된 후보가 0개 — 기존 candidates 는 건드리지 않음", rd)
            return 0

        names = _names()
        rows = [
            (rd, r.symbol, names.get(r.symbol), i, float(r.ret_5d), int(r.above_ma20))
            for i, r in enumerate(out.itertuples(), start=1)
        ]
        conn.execute("DELETE FROM candidates WHERE run_date = ?", (rd,))
        conn.executemany(
            "INSERT INTO candidates (run_date, symbol, name, momentum_rank, ret_5d, above_ma20) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
    log.info("[%s] candidates %d개 저장: %s", rd, len(rows), [r[1] for r in rows])
    return len(rows)


if __name__ == "__main__":
    import sys

    run(sys.argv[1] if len(sys.argv) > 1 else None)
