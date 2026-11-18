import argparse
from datetime import date

from candidate_selector import selector
from common.config import get_config
from common.db import init_db
from common.logger import get_logger
from data_collector import collector, disclosure
from exit_monitor import monitor
from market_regime import regime
from news_judge import judge
from order_manager import decision_prices, manager, temp_rule

log = get_logger("main")
CFG = get_config()


def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    return d.weekday() < 5 and d.isoformat() not in (CFG["schedule"].get("holidays") or [])


def _safe(step):
    def wrapper():
        if not is_trading_day():
            return
        try:
            step()
        except Exception:
            log.exception("%s 실패", step.__name__)
    wrapper.__name__ = step.__name__
    return wrapper


def _optional(step, what: str) -> None:
    try:
        step()
    except Exception:
        log.exception("%s 실패 — 이 단계 없이 계속", what)


def premarket_job():
    collector.update_daily_candles()
    selector.run()
    _optional(regime.run, "시장 국면 판정")


def judge_job():
    collector.collect_news()
    _optional(disclosure.run, "공시 수집")
    if CFG["llm"]["use_llm"]:
        judge.run()
    else:
        temp_rule.run()
    decision_prices.run()
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
    scheduler = BlockingScheduler(
        timezone=tz,
        executors={"default": ThreadPoolExecutor(1)},
        job_defaults={"coalesce": True, "misfire_grace_time": 300},
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
