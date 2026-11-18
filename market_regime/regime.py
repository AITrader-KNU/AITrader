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
    r = _rcfg(cfg or get_config())
    table = r["max_equity_ratio"]
    key = regime if regime in table else r["default_when_missing"]
    return float(table[key])


def build_index(daily: pd.DataFrame, cfg: dict) -> pd.DataFrame:
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
