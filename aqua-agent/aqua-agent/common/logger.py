"""날짜별 로그 파일 + 콘솔 출력."""
import logging
from datetime import datetime
from pathlib import Path

from common.config import ROOT, get_config


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    cfg = get_config()["log"]
    log_dir = ROOT / cfg["dir"]
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.FileHandler(log_dir / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.setLevel(cfg.get("level", "INFO"))
    return logger
