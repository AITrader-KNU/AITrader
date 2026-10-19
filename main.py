"""에이전트 진입점 — 학교 서버에서 이 파일 하나를 계속 띄워 둡니다.

  python main.py              # 스케줄러 실행 (평일 장 시간에 자동으로 돎)
  python main.py --once all   # 지금 바로 전체 흐름을 한 번 돌려 보기 (테스트용)
  python main.py --once premarket | judge | exit

하루 흐름 (시각은 config.yaml 의 schedule)
  08:00              데이터 갱신 → 후보 선정
  09:30·11:00·13:00  뉴스 수집 → 판단(LLM) → 주문
  09:00~15:00 15분마다 청산 감시 → 주문
에이전트끼리는 DB 테이블로만 데이터를 주고받으므로, 여기서는 순서만 정합니다.
"""
import argparse
from datetime import date

from candidate_selector import selector
from common.config import get_config
from common.db import init_db
from common.logger import get_logger
from data_collector import collector
from exit_monitor import monitor
from news_judge import judge
from order_manager import manager

log = get_logger("main")
CFG = get_config()


def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    return d.weekday() < 5 and d.isoformat() not in (CFG["schedule"].get("holidays") or [])


def _safe(step):
    """한 단계가 실패해도 다음 사이클은 계속 돌도록 예외를 로그로만 남긴다."""
    def wrapper():
        if not is_trading_day():
            return
        try:
            step()
        except Exception:
            log.exception("%s 실패", step.__name__)
    wrapper.__name__ = step.__name__
    return wrapper


def premarket_job():
    collector.update_daily_candles()
    selector.run()


def judge_job():
    collector.collect_news()
    judge.run()
    manager.run()


def exit_job():
    monitor.run()
    manager.run()


JOBS = {"premarket": premarket_job, "judge": judge_job, "exit": exit_job}


def run_scheduler():
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.schedulers.blocking import BlockingScheduler

    sch = CFG["schedule"]
    tz = CFG["timezone"]
    # 작업을 한 번에 하나씩만 실행한다. 09:30·11:00·13:00에는 판단 작업과 청산 작업이
    # 같은 시각에 걸리는데, 동시에 돌면 주문 관리자가 두 번 실행돼 같은 요청을 두 번 주문할 수 있다.
    scheduler = BlockingScheduler(
        timezone=tz,
        executors={"default": ThreadPoolExecutor(1)},
        job_defaults={"coalesce": True, "misfire_grace_time": 300},  # 밀린 작업은 5분 안이면 실행
    )

    h, m = sch["premarket"].split(":")
    scheduler.add_job(_safe(premarket_job), "cron", day_of_week="mon-fri", hour=h, minute=m)

    for t in sch["reevaluate"]:
        h, m = t.split(":")
        scheduler.add_job(_safe(judge_job), "cron", day_of_week="mon-fri", hour=h, minute=m)

    open_h = int(sch["market_open"].split(":")[0])
    close_h = int(sch["market_close"].split(":")[0])
    scheduler.add_job(_safe(exit_job), "cron", day_of_week="mon-fri",
                      hour=f"{open_h}-{close_h - 1}", minute=f"*/{sch['exit_check_minutes']}")

    log.info("스케줄러 시작 (%s)", tz)
    scheduler.start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", choices=[*JOBS, "all"], help="지금 한 번만 실행")
    args = parser.parse_args()

    init_db()
    if args.once == "all":
        for job in JOBS.values():
            job()
    elif args.once:
        JOBS[args.once]()
    else:
        run_scheduler()


if __name__ == "__main__":
    main()
