"""main.py 가 각 담당의 실제 구현을 부르는지 확인. 실행: python -m pytest tests/test_main_wiring.py"""
import main


def test_exit_job_calls_implemented_monitor():
    # 청산 감시자 구현(run_once·evaluate)이 main.py 가 부르는 exit_monitor/monitor.py 에 있어야 한다
    assert callable(getattr(main.monitor, "run_once", None))
    assert callable(getattr(main.monitor, "evaluate", None))


def test_selector_values_are_in_config():
    # 수치는 config.yaml 에만 둔다(selector.py 는 기본값 없이 config 에서 읽음)
    from common.config import get_config
    cfg = get_config()
    assert "max_daily_move" in cfg["universe"]
    assert {"ma_slope_days", "rank_among"} <= cfg["candidates"].keys()
    assert {"weight_return", "weight_high"} <= cfg["candidates"]["momentum"].keys()
