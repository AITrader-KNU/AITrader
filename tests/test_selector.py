import sqlite3
import numpy as np
import pandas as pd
import pytest

from common import db as dbm
from common.config import get_config
from candidate_selector import selector

RD = "2026-10-23"
N = 300


@pytest.fixture
def conn(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    monkeypatch.setattr(dbm, "db_path", lambda: p)
    dbm.init_db()
    return p


def series(kind, n=N, base=10000.0):
    t = np.arange(n)
    if kind == "up":      # 꾸준한 상승 → 후보
        c = base * (1 + 0.003) ** t
    elif kind == "down":  # 하락 → 추세 탈락
        c = base * (1 - 0.002) ** t
    elif kind == "hot":   # 상승 후 마지막 날 급등 → 과열
        c = base * (1 + 0.003) ** t
        c[-1] = c[-2] * 1.12
    elif kind == "split":  # 분할 의심(하루 -50%)
        c = base * (1 + 0.003) ** t
        c[150:] *= 0.5
    return c


def load(path, specs, vol=1_000_000, n=N):
    dates = pd.bdate_range(end=pd.Timestamp(RD) - pd.Timedelta(days=1), periods=n).strftime("%Y-%m-%d")
    rows = []
    for sym, (kind, v) in specs.items():
        c = series(kind, n)
        for d, x in zip(dates, c):
            rows.append((sym, d, int(x), int(x * 1.01), int(x * 0.99), int(x), int(v)))
    # 오늘 봉(제외돼야 함): 폭등
    rows.append(("UP1", RD, 1, 1, 1, 1, 1))
    with sqlite3.connect(path) as c:
        c.executemany("INSERT INTO daily_candles VALUES (?,?,?,?,?,?,?)", rows)


def cands(path):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT symbol, momentum_rank, ret_5d, above_ma20 FROM candidates WHERE run_date=? ORDER BY momentum_rank", (RD,)).fetchall()


def test_filters_and_rank(conn):
    load(conn, {"UP1": ("up", 1e6), "UP2": ("up", 2e6), "DOWN": ("down", 1e6), "HOT": ("hot", 1e6), "SPLIT": ("split", 1e6)})
    assert selector.run(RD) == 2
    r = cands(conn)
    assert {x[0] for x in r} == {"UP1", "UP2"}
    assert [x[1] for x in r] == [1, 2]
    assert all(x[3] == 1 for x in r)


def test_today_bar_excluded(conn):
    load(conn, {"UP1": ("up", 1e6)})
    selector.run(RD)  # 오늘 봉(close=1)이 쓰였다면 추세/일일수익 때문에 탈락했을 것
    assert [x[0] for x in cands(conn)] == ["UP1"]


def test_idempotent_and_no_overwrite_when_empty(conn):
    load(conn, {"UP1": ("up", 1e6), "UP2": ("up", 2e6)})
    selector.run(RD); selector.run(RD)
    assert len(cands(conn)) == 2
    assert selector.run("2020-01-01") == 0  # 봉 없음
    assert len(cands(conn)) == 2


def test_short_history_skipped(conn):
    load(conn, {"UP1": ("up", 1e6)}, n=100)
    assert selector.run(RD) == 0


def test_top_n_and_liquidity(conn, monkeypatch):
    load(conn, {f"S{i}": ("up", 1e6 * (i + 1)) for i in range(20)})
    cfg = get_config()
    assert selector.run(RD) == cfg["candidates"]["top_n"]
    r = cands(conn)
    assert len(r) == 15 and [x[1] for x in r] == list(range(1, 16))


def test_ret5d_value(conn):
    load(conn, {"UP1": ("up", 1e6)})
    selector.run(RD)
    assert cands(conn)[0][2] == pytest.approx(1.003 ** 5 - 1, rel=0.02)
