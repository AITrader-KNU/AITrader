"""4-3 뉴스 분석·판단 에이전트 (LLM) — 담당: NLP·LLM

읽는 것: candidates, news 테이블, 포트폴리오(보유 종목)
쓰는 테이블: decisions (종목마다 한 행, status='NEW')

Gemini 출력 형식(JSON) — 형식이 깨지거나 필드가 빠지면 해당 종목은 WAIT 로 저장
  {"symbol": "005930", "action": "BUY | WAIT | SELL", "bad_news": false,
   "reason": "...", "risks": ["..."], "news_ids": ["..."]}
"""
from common.logger import get_logger

log = get_logger("news_judge")


def run() -> None:
    """후보·보유 종목을 한 번의 Gemini 호출로 판단해 decisions 에 저장.
    - 판단 시각 이후 게시된 기사는 입력에서 제외(미래 정보 방지)
    - 호출마다 입력·출력 토큰 수를 로그로 남기기(비용 추적)
    TODO(NLP·LLM)"""
    log.info("news_judge.run: TODO")
