import copy

import numpy as np
import pandas as pd
import pytest

from common import db as dbm
from common.config import get_config
from market_regime import regime as rg

RD = "2026-10-23"
N = 150


def cfg(**regime):
    c = copy.deepcopy(get_config())
    c["regime"] = {"top_n": 10, "min_stocks": 8, **regime}
    return c


# ---------------------------------------------------------------- classify (순수 함수)
def path(kind, n=120, base=100.0):
    t = np.arange(n)
    if kind == "up":
        return base * 1.004 ** t
    if kind == "down":
        return base * 0.996 ** t
    if kind == "side":   # 일정한 값 주변에서 오르내림
        return base * (1 + 0.01 * np.sin(t / 3.0))
    raise ValueError(kind)


def test_rising_is_up():
    assert rg.classify(path("up")) == rg.UP


def test_falling_is_down():
    assert rg.classify(path("down")) == rg.DOWN


def test_sideways():
    assert rg.classify(path("side")) == rg.SIDEWAYS


def test_fewer_than_60_bars_is_unknown():
    assert rg.classify(path("up", n=59)) == rg.UNKNOWN
    assert rg.classify([]) == rg.UNKNOWN


def test_exactly_60_bars_is_classified():
    assert rg.classify(path("up", n=60)) == rg.UP


def test_bad_values_are_unknown():
    x = path("up")
    x[-5] = np.nan
    assert rg.classify(x) == rg.UNKNOWN
    y = path("up")
    y[-1] = 0
    assert rg.classify(y) == rg.UNKNOWN


def test_scale_invariant():
    x = path("down")
    assert rg.classify(x) == rg.classify(x * 7.3)


def _tail(down_bars=100, rebound_bars=8, rebound=0.02):
    x = list(100.0 * 0.996 ** np.arange(down_bars))
    for _ in range(rebound_bars):
        x.append(x[-1] * (1 + rebound))
    return np.array(x)


def test_rebound_inside_downtrend_is_still_down():
    # 문서: 하락 = 종가 < 60일선 이고 20일선 < 60일선 (종가가 20일선 위여도 하락)
    x = _tail(rebound_bars=5)
    assert x[-1] > x[-20:].mean()                 # 종가는 20일선 위
    assert x[-1] < x[-60:].mean()                 # 하지만 60일선 아래
    assert x[-20:].mean() < x[-60:].mean()
    assert rg.classify(x) == rg.DOWN


def test_above_long_ma_but_short_ma_below_is_sideways():
    x = _tail(rebound_bars=6)                     # 종가가 60일선을 넘었지만 20일선은 아직 60일선 아래
    assert x[-1] > x[-60:].mean()
    assert x[-20:].mean() < x[-60:].mean()
    assert rg.classify(x) == rg.SIDEWAYS


def test_up_needs_price_above_short_ma():
    # 20일선 > 60일선 이어도 종가가 20일선 아래면 상승이 아님
    x = list(100.0 * 1.004 ** np.arange(120))
    x[-1] = x[-1] * 0.9
    assert x[-1] < np.mean(x[-20:]) and np.mean(x[-20:]) > np.mean(x[-60:])
    assert rg.classify(x) == rg.SIDEWAYS


def test_accepts_plain_list():
    assert rg.classify(list(path("up"))) == rg.UP


# ---------------------------------------------------------------- 지수 만들기
def make_daily(n_stocks=12, n=N, drift=0.003, **override):
    dates = pd.bdate_range(end=pd.Timestamp(RD) - pd.Timedelta(days=1), periods=n).strftime("%Y-%m-%d")
    rows = []
    for k in range(n_stocks):
        sym = f"{k:06d}"
        close = 10000.0 * (1 + drift) ** np.arange(n)
        vol = np.full(n, 1_000_000.0) * (1 + k * 0.01)
        if sym in override:
            close, vol = override[sym](close.copy(), vol.copy())
        for d, c, v in zip(dates, close, vol):
            rows.append((sym, d, c, v))
    return pd.DataFrame(rows, columns=["symbol", "date", "close", "volume"])


def test_equal_weight_index_return():
    df = make_daily(drift=0.003)
    ix = rg.build_index(df, cfg())
    assert ix["ret"].iloc[-1] == pytest.approx(0.003)          # 모든 종목이 +0.3% → 평균도 +0.3%
    assert ix["n_stocks"].iloc[-1] == 10                       # top_n=10 개만


def test_membership_uses_previous_days_only():
    # 마지막 날 거래량이 폭증한 종목은 그날 종목군에 들어오면 안 됨(전날까지 평균으로 고름)
    def spike(c, v):
        v[-1] = 1e12
        c[-1] = c[-2] * 1.2
        return c, v
    df = make_daily(n_stocks=12, **{"000000": spike})          # 000000 은 원래 거래대금 최하위
    ix = rg.build_index(df, cfg())
    assert ix["ret"].iloc[-1] == pytest.approx(0.003)          # +20% 종목이 섞이지 않음


def test_big_move_excluded():
    def split(c, v):
        c[100:] *= 0.5                                          # 하루 -50% (분할 의심)
        return c, v
    df = make_daily(n_stocks=12, **{"000011": split})           # 거래대금 최상위(종목군에 포함)
    ix = rg.build_index(df, cfg())
    day = sorted(df["date"].unique())[100]
    assert ix.loc[day, "ret"] == pytest.approx(0.003)           # 제외되어 평균이 흔들리지 않음
    assert ix.loc[day, "n_stocks"] == 9


def test_zero_volume_excluded():
    def halted(c, v):
        v[-1] = 0
        return c, v
    df = make_daily(n_stocks=12, **{"000011": halted})
    ix = rg.build_index(df, cfg())
    assert ix["n_stocks"].iloc[-1] == 9


def test_regimes_follow_market():
    up = rg.build_regimes(make_daily(drift=0.003), cfg())
    down = rg.build_regimes(make_daily(drift=-0.003), cfg())
    assert up["regime"].iloc[-1] == rg.UP
    assert down["regime"].iloc[-1] == rg.DOWN
    assert (up["regime"].iloc[:50] == rg.UNKNOWN).all()         # 지수 60개가 쌓이기 전


def test_low_stock_days_dropped():
    def sparse(c, v):
        v[-1] = 0
        return c, v
    ov = {f"{k:06d}": sparse for k in range(12)}
    reg = rg.build_regimes(make_daily(n_stocks=12, **ov), cfg())
    assert reg.index.max() < sorted(make_daily(n_stocks=1)["date"])[-1]   # 마지막 날은 빠짐


def test_equity_cap_uses_document_defaults():
    c = copy.deepcopy(get_config())
    c.pop("regime", None)
    assert rg.equity_cap(rg.UP, c) == 0.9
    assert rg.equity_cap(rg.SIDEWAYS, c) == 0.6
    assert rg.equity_cap(rg.DOWN, c) == 0.3


def test_equity_cap_from_config_and_missing_fallback():
    c = copy.deepcopy(get_config())
    c["regime"] = {"max_equity_ratio": {"up": 1.0, "sideways": 0.5, "down": 0.2}, "default_when_missing": "sideways"}
    assert rg.equity_cap("down", c) == 0.2
    assert rg.equity_cap(rg.UNKNOWN, c) == 0.5        # 국면을 모르면 횡보 값
    assert rg.equity_cap("엉뚱한값", c) == 0.5


# ---------------------------------------------------------------- run (DB)
@pytest.fixture
def conn(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    monkeypatch.setattr(dbm, "db_path", lambda: p)
    monkeypatch.setattr(rg, "get_config", lambda: cfg())
    dbm.init_db()
    return p


def save(path, df):
    import sqlite3
    with sqlite3.connect(path) as c:
        c.executemany(
            "INSERT INTO daily_candles (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
            [(r.symbol, r.date, r.close, r.close, r.close, r.close, r.volume) for r in df.itertuples()],
        )


def read(path):
    import sqlite3
    with sqlite3.connect(path) as c:
        return c.execute(
            "SELECT run_date, regime, index_close, ma_short, ma_long, max_equity_ratio, created_at FROM market_regime"
        ).fetchall()


def test_run_writes_and_is_idempotent(conn):
    save(conn, make_daily(drift=0.003))
    assert rg.run(RD) == rg.UP
    assert rg.run(RD) == rg.UP
    rows = read(conn)
    assert len(rows) == 1
    run_date, regime, close, ma_s, ma_l, ratio, created = rows[0]
    assert (run_date, regime, ratio) == (RD, "up", 0.9)
    assert close > ma_s > ma_l > 0 and created.startswith("20")


def test_run_stores_ratio_per_regime(conn):
    save(conn, make_daily(drift=-0.003))
    assert rg.run(RD) == rg.DOWN
    assert read(conn)[0][1:] [0] == "down" and read(conn)[0][5] == 0.3


def test_run_ignores_run_date_bar(conn):
    df = make_daily(drift=0.003)
    last = df["date"].max()
    save(conn, df)
    rg.run(last)                                                # last 날짜 봉은 쓰지 않음
    expect = rg.build_regimes(df[df["date"] < last], cfg())["index_close"].iloc[-1]
    assert read(conn)[0][2] == pytest.approx(expect)


def test_run_without_data_keeps_table(conn):
    assert rg.run(RD) == rg.UNKNOWN
    assert read(conn) == []


def test_run_with_few_bars_writes_nothing(conn):
    save(conn, make_daily(n=40))
    assert rg.run(RD) == rg.UNKNOWN
    assert read(conn) == []                                     # 기록 없음 → 주문 관리자가 default_when_missing 적용
