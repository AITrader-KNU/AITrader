"""config.yaml 과 .env 를 읽는 곳. 다른 모듈은 여기서만 설정을 가져옵니다."""
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@lru_cache(maxsize=1)
def get_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str, default: str | None = None) -> str | None:
    """비밀값(.env)을 읽는다. 코드나 config.yaml 에 비밀값을 쓰지 않기."""
    return os.getenv(name, default)
