"""시장 국면 판정 — 매일 장 시작 전(premarket) 후보 선정 전에 실행.

흐름: 일봉 → 지수(유동성 상위 N종목 동일가중) → classify(종가 목록) → market_regime 테이블.
계산(classify, build_index, build_regimes)은 순수 함수라 API·DB 없이 테스트할 수 있고, run() 만 DB 를 읽고 쓴다.
(후보 선정기의 compute / run 과 같은 구조)

지수(KODEX 200 대신): 거래대금 가중은 삼성전자·SK하이닉스 쏠림이 커서 쓰지 않고, 후보를 동일 비중으로 사는 것과 맞게 동일가중.
  - 종목: 날짜 t 마다 't 전날까지' 20일 평균 거래대금 상위 regime.top_n (당일 값으로 고르면 미래 정보)
  - 지수(t) = 지수(t-1) × (1 + 그날 종가 수익률의 단순 평균)
  - 수익률을 못 구하는 종목(전일/당일 종가 없음, 거래량 0)과 하루 ±universe.max_daily_move 초과 종목은 그날 제외
  - 그날 유효 종목이 regime.min_stocks 미만이면 그날은 지수에 넣지 않고, 국면은 직전 값을 유지(경고 로그)

국면(classify, 설계 문서 1절 '판정 규칙', 전일 종가 기준): 지수 종가 목록(과거→최근)만 받는다.
  up        종가 > 20일선  이고  20일선 > 60일선
  down      종가 < 60일선  이고  20일선 < 60일선
  sideways  상승도 하락도 아님
  unknown   봉이 60개 미만이거나 값이 이상함 → market_regime 에 쓰지 않음
            (주문 관리자가 '그날 기록 없음' 으로 보고 default_when_missing 을 씀 — 문서 동작 흐름 3)

주식 비중 상한: regime.max_equity_ratio[국면] (문서 초깃값 up 0.9 / sideways 0.6 / down 0.3).
  그날 값을 market_regime.max_equity_ratio 에 같이 저장해서, 나중에 설정이 바뀌어도 과거 기록이 그대로 남는다.
  이 상한을 예산에 반영하는 곳은 order_manager/manager.py (이 모듈은 값을 계산해 저장만 한다).

읽는 곳: daily_candles (run_date 이전 봉만 사용)
쓰는 곳: market_regime (run_date, regime, index_close, ma_short, ma_long, max_equity_ratio, created_at)
         — 문서 4절 열과 같음. 이 모듈이 없으면 만듦(common/db.py 에도 같은 CREATE TABLE IF NOT EXISTS 를 넣을 것)

config.yaml 의 regime 섹션 (문서 + 이 지수 방식에서 추가한 top_n, min_stocks). 없으면 아래 기본값:
  regime:
    enabled: true                  # 주문 관리자가 읽음. false 면 상한 없이 기존처럼 매수
    ma_short: 20
    ma_long: 60
    max_equity_ratio: {up: 0.9, sideways: 0.6, down: 0.3}
    default_when_missing: sideways
    top_n: 200                     # 지수에 쓰는 유동성 상위 종목 수 (KODEX 200 을 못 받아 대신 만든 지수)
    min_stocks: 150                # 그날 유효 종목이 이보다 적으면 그날은 지수에서 뺌
    # index_symbol: "069500"       # 사용하지 않음 (AQuA 에서 KODEX 200 일봉을 받을 수 없음)
"""
from __future__ import annotations

import argparse
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from common.config import get_config
from common.db import connect, init_db
from common.logger import get_logger

log = get_logger("regime")

UP, SIDEWAYS, DOWN, UNKNOWN = "up", "sideways", "down", "unknown"   # config 의 max_equity_ratio 키와 같음
STALE_DAYS = 4  # 마지막 봉이 run_date 보다 이만큼 이상 오래됐으면 경고(연휴 감안)

DEFAULTS = {"top_n": 200, "min_stocks": 150, "ma_short": 20, "ma_long": 60,
            "max_equity_ratio": {"up": 0.9, "sideways": 0.6, "down": 0.3}, "default_when_missing": "sideways"}

TABLE = """
CREATE TABLE IF NOT EXISTS market_regime (
    run_date         TEXT PRIMARY KEY,   -- 이 국면을 적용하는 날짜
    regime           TEXT NOT NULL CHECK (regime IN ('up', 'sideways', 'down')),
    index_close      REAL,               -- 판정에 쓴 마지막 날(run_date 이전)의 지수
    ma_short         REAL,
    ma_long          REAL,
    max_equity_ratio REAL NOT NULL,      -- 그날의 주식 비중 상한(예: 0.6)
    created_at       TEXT NOT NULL       -- ISO-8601 KST
);
"""


def _rcfg(cfg: dict | None) -> dict:
    r = dict(DEFAULTS)
    r.update((cfg or {}).get("regime") or {})
    r.setdefault("min_bars", r["ma_long"])
    return r


# ----------------------------------------------------------------------------- 순수 함수
def classify(closes, cfg: dict | None = None) -> str:
    """지수 종가 목록(과거→최근) → "up" | "sideways" | "down" | "unknown". API·DB 를 쓰지 않는다."""
    r = _rcfg(cfg)
    x = np.asarray(list(closes), dtype="float64")
    if len(x) < r["min_bars"] or len(x) < r["ma_long"]:
        return UNKNOWN
    tail = x[-r["ma_long"]:]
    if not np.isfinite(tail).all() or (tail <= 0).any():
        return UNKNOWN
    last = x[-1]
    short = x[-r["ma_short"]:].mean()
    long_ = tail.mean()
    if last > short and short > long_:          # 상승: 종가 > 20일선, 20일선 > 60일선
        return UP
    if last < long_ and short < long_:          # 하락: 종가 < 60일선, 20일선 < 60일선
        return DOWN
    return SIDEWAYS


def equity_cap(regime: str, cfg: dict | None = None) -> float:
    """국면 → 주식 비중 상한(0~1). 국면을 모르면(unknown 등) default_when_missing 의 값."""
    r = _rcfg(cfg or get_config())
    table = r["max_equity_ratio"]
    key = regime if regime in table else r["default_when_missing"]
    return float(table[key])


def build_index(daily: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """일봉(symbol,date,close,volume) → 날짜별 [ret, n_stocks] (지수 일간 수익률과 유효 종목 수).

    날짜 t 의 종목군은 t 전날까지의 20일 평균 거래대금 상위 top_n (미래 정보 배제).
    """
    r = _rcfg(cfg)
    liq_w = cfg["universe"]["liquidity_window"]
    max_move = cfg.get("universe", {}).get("max_daily_move", 0.30)
    close = daily.pivot(index="date", columns="symbol", values="close").sort_index().astype("float64")
    vol = daily.pivot(index="date", columns="symbol", values="volume").sort_index().astype("float64")
    liq = (close * vol).rolling(liq_w).mean().shift(1)             # 전날까지
    member = liq.rank(axis=1, ascending=False, method="first") <= r["top_n"]
    ret = close / close.shift(1) - 1
    ok = member & ret.notna() & (vol > 0) & (ret.abs() <= max_move)
    ret = ret.where(ok)
    return pd.DataFrame({"ret": ret.mean(axis=1), "n_stocks": ok.sum(axis=1)})


def build_regimes(daily: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """일봉 → 날짜별 [index_close, ma_short, ma_long, n_stocks, regime]. (그 날짜 종가까지의 정보만 사용)"""
    r = _rcfg(cfg)
    ix = build_index(daily, cfg)
    good = ix[(ix["n_stocks"] >= r["min_stocks"]) & ix["ret"].notna()].copy()
    if good.empty:
        return pd.DataFrame(columns=["index_close", "ma_short", "ma_long", "n_stocks", "regime"])
    good["index_close"] = 100.0 * (1 + good["ret"]).cumprod()
    v = good["index_close"].to_numpy()
    good["ma_short"] = good["index_close"].rolling(r["ma_short"]).mean()
    good["ma_long"] = good["index_close"].rolling(r["ma_long"]).mean()
    good["regime"] = [classify(v[: i + 1][-r["ma_long"]:], cfg) for i in range(len(v))]
    skipped = ix.index.difference(good.index)
    if len(skipped):
        log.warning("유효 종목 %d개 미만이라 지수에서 뺀 날짜 %d일: %s", r["min_stocks"], len(skipped), list(skipped)[-5:])
    return good[["index_close", "ma_short", "ma_long", "n_stocks", "regime"]]


# ----------------------------------------------------------------------------- DB
def _load_candles(conn, run_date: str, max_bars: int | None = None) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT symbol, date, close, volume FROM daily_candles WHERE date < ? ORDER BY symbol, date",
        conn, params=(run_date,),
    )
    if df.empty:
        return df
    for c in ("close", "volume"):
        df[c] = df[c].astype("float64")  # int 곱셈 오버플로 방지
    return df.groupby("symbol", group_keys=False).tail(max_bars) if max_bars else df


def run(run_date: date | str | None = None) -> str:
    """국면을 판정해 market_regime 에 그날 한 행을 쓰고 국면 문자열을 반환. 같은 run_date 는 덮어씀(멱등).

    일봉이 없거나 봉이 60개 미만이면 기존 행을 건드리지 않고 "unknown" 만 반환(쓰지 않음).
    → 주문 관리자는 '그날 기록 없음' 으로 보고 default_when_missing(횡보 60%)을 적용한다.
    """
    init_db()
    cfg = get_config()
    r = _rcfg(cfg)
    rd = (run_date or date.today()).isoformat() if not isinstance(run_date, str) else run_date
    max_bars = max(r["ma_long"], cfg["universe"]["liquidity_window"]) + r["min_bars"] + 30

    with connect() as conn:
        conn.executescript(TABLE)
        df = _load_candles(conn, rd, max_bars)
        if df.empty:
            log.error("[%s] daily_candles 에 봉이 없습니다 — market_regime 은 건드리지 않음", rd)
            return UNKNOWN
        last = df["date"].max()
        if (date.fromisoformat(rd) - date.fromisoformat(last)).days > STALE_DAYS:
            log.warning("[%s] 최신 봉이 %s 입니다 — 데이터 갱신이 안 됐을 수 있음", rd, last)
        reg = build_regimes(df, cfg)
        if reg.empty or reg["regime"].iloc[-1] == UNKNOWN:
            log.warning("[%s] 국면을 판정할 수 없습니다(지수 봉 %d개 < %d) — market_regime 에 쓰지 않음",
                        rd, len(reg), r["min_bars"])
            return UNKNOWN
        row = reg.iloc[-1]
        asof = reg.index[-1]
        if asof != last:
            log.warning("[%s] 마지막 봉 %s 는 유효 종목 부족 → %s 기준 국면 사용", rd, last, asof)
        regime = str(row["regime"])
        ratio = equity_cap(regime, cfg)
        now = datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Seoul"))).isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO market_regime (run_date, regime, index_close, ma_short, ma_long, max_equity_ratio, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (rd, regime, float(row["index_close"]),
             None if math.isnan(row["ma_short"]) else float(row["ma_short"]),
             None if math.isnan(row["ma_long"]) else float(row["ma_long"]),
             ratio, now),
        )
    log.info("[%s] 국면 %s (기준일 %s, 지수 %.2f, 종목 %d개), 주식 비중 상한 %.0f%%", rd, regime, asof,
             row["index_close"], row["n_stocks"], ratio * 100)
    return regime


# ----------------------------------------------------------------------------- 확인용 출력
def print_history(start: str, end: str) -> None:
    """start~end 날짜별 국면을 출력(조회 전용). '기준일' 종가까지 보고 판정한 값 → 다음 거래일에 적용."""
    cfg = get_config()
    with connect() as conn:
        df = _load_candles(conn, "9999-12-31")
    reg = build_regimes(df, cfg)
    sel = reg.loc[start:end]
    if sel.empty:
        print("해당 기간 결과 없음"); return
    print(f"{'기준일':<11}{'지수':>9}{'20일선':>9}{'60일선':>9}{'종목수':>6}  국면")
    for d, x in sel.iterrows():
        f = lambda v: f"{v:9.2f}" if pd.notna(v) else f"{'-':>9}"
        print(f"{d:<11}{x['index_close']:9.2f}{f(x['ma_short'])}{f(x['ma_long'])}{int(x['n_stocks']):6d}  {x['regime']}")
    flips = int((sel["regime"] != sel["regime"].shift()).sum() - 1)
    print("\n국면별 일수:", {k: int(v) for k, v in sel["regime"].value_counts().items()})
    print(f"국면 바뀐 횟수 {flips}회, 유효 종목 수 최소 {int(sel['n_stocks'].min())}개")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("run_date", nargs="?", default=None, help="판정 적용일 (기본: 오늘)")
    ap.add_argument("--history", nargs=2, metavar=("FROM", "TO"), help="기간별 국면 출력(저장 안 함). 예: --history 2026-07-01 2026-10-31")
    a = ap.parse_args()
    if a.history:
        print_history(*a.history)
    else:
        print(run(a.run_date))
