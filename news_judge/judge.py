"""4-3 뉴스 분석·판단 에이전트 (LLM) — 담당: NLP·LLM

읽는 것: candidates, news, disclosures(DART 공시) 테이블, positions_meta(보유 종목)
쓰는 테이블: decisions (종목마다 한 행, status='NEW')
"""
import json
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Set

from common.logger import get_logger
from common.config import get_config, env
from common.db import connect

# common.db에 STATUS_NEW가 없을 경우를 대비한 안전 장치
try:
    from common.db import STATUS_NEW
except ImportError:
    STATUS_NEW = "NEW"

log = get_logger("news_judge")
KST = timezone(timedelta(hours=9))

SYSTEM_INSTRUCTION = """
당신은 한국 주식 퀀트 트레이딩 에이전트의 뉴스 분석 및 매매 판단 엔진입니다.
제공된 종목 정보와 최근 뉴스 헤드라인만을 근거로 객관적인 판단을 내려야 합니다.

[핵심 매매 원칙 및 피드백 규칙]
1. 미래 정보 차단: 기준 시각 이전의 뉴스만 고려하며, 제공되지 않은 외부 정보로 지레짐작하지 마십시오.
2. 미보유 종목 (후보군):
   - 대형 수주, 실적 호조, 신사업 확장 등 명확한 호재가 있으면 BUY를 검토하십시오.
   - 횡령, 배임, 소송, 일반공모 유상증자, 분식회계 등 악재 발생 시 action은 WAIT, bad_news는 true로 설정하여 신규 매수를 금지하십시오.
   - 뉴스가 없거나 모호한 찌라시인 경우 WAIT로 판단하십시오.
3. 보유 중인 종목:
   - [필수 규칙] 보유 중인 종목에 확인된 악재(소송, 유상증자, 횡령, 실적 쇼크 등)가 발생한 경우, 반드시 action을 SELL로 내리고 bad_news를 true로 설정하십시오.
   - 특별한 악재가 없거나 상승 추세가 유지된다면 WAIT(홀딩 유지)로 판단하십시오.
4. 공시 우선: '최근 공시'는 회사가 금융감독원 DART 에 직접 낸 공식 자료이므로 뉴스보다 신뢰도가 높은 근거로 보십시오.
   뉴스가 호재여도 유상증자·소송·횡령·감사의견 등 악재 공시가 있으면 악재로 판단하십시오. 근거로 쓴 공시는 news_ids 에 "D:접수번호" 형식으로 넣으십시오.
5. 출력 형식: 반드시 마크다운 기호 없이 순수 JSON 리스트 포맷으로만 출력하십시오.
"""

def get_current_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")

def call_gemini_api(prompt: str, model_name: str, api_key: str) -> Dict[str, Any]:
    clean_model = model_name.replace("models/", "")
    # API 키는 URL 이 아니라 헤더로 보낸다(Gemini API 문서의 x-goog-api-key 방식).
    # URL 에 넣으면 타임아웃 등 requests 오류 메시지에 URL 이 키째로 담겨 로그에 남는다.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}
    }
    timeout = get_config().get("llm", {}).get("timeout_seconds", 30)   # 응답 대기(초), config.yaml llm.timeout_seconds
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if response.status_code != 200:
        raise Exception(f"HTTP {response.status_code}: {response.text}")
    res_json = response.json()
    text_content = res_json["candidates"][0]["content"]["parts"][0]["text"]
    usage_metadata = res_json.get("usageMetadata", {})
    return {
        "text": text_content,
        "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
        "candidates_tokens": usage_metadata.get("candidatesTokenCount", 0),
        "total_tokens": usage_metadata.get("totalTokenCount", 0)
    }

def _recent_disclosures(cursor, symbol: str, run_at: str, limit: int) -> List[Dict[str, Any]]:
    """판단 시각 이전에 수집된 최근 공시(미래 정보 차단: collected_at <= run_at).

    공시 접수일(rcept_dt)에는 시각이 없어서 수집 시각으로 '판단 시점에 알 수 있었는지'를 가린다.
    collected_at 은 마이크로초까지, run_at 은 초 단위(소수점 버림)라 문자열이 아니라 시각으로 비교하고,
    run_at 이 실제 시각보다 최대 1초 이르게 적혀 있으므로 run_at + 1초 미만까지 포함한다
    (공시 수집은 판단 시작 전에 끝나므로 같은 초에 수집된 공시도 판단 전에 이미 있던 것).
    disclosures 테이블이 없거나(공시 수집 전 DB) 조회가 실패하면 공시 없이 판단한다.
    """
    if limit <= 0:
        return []
    try:
        cutoff = datetime.fromisoformat(run_at) + timedelta(seconds=1)
        cursor.execute("""
            SELECT rcept_no, report_nm, rcept_dt, collected_at FROM disclosures
            WHERE symbol = ? ORDER BY rcept_dt DESC, rcept_no DESC
        """, (symbol,))
        rows = [r for r in cursor.fetchall() if datetime.fromisoformat(r[3]) < cutoff]
        return [{"rcept_no": r[0], "report_nm": r[1], "rcept_dt": r[2]} for r in rows[:limit]]
    except (sqlite3.Error, ValueError) as e:
        log.warning(f"news_judge.run: 공시 조회 실패({type(e).__name__}) — {symbol} 은 공시 없이 판단")
        return []

def validate_decision_item(item: Any, symbol: str) -> Dict[str, Any]:
    required_fields = ["symbol", "action", "bad_news", "reason", "risks", "news_ids"]
    if not isinstance(item, dict):
        return {"symbol": symbol, "action": "WAIT", "bad_news": False, "reason": "형식 오류", "risks": [], "news_ids": []}
    for field in required_fields:
        if field not in item:
            return {"symbol": symbol, "action": "WAIT", "bad_news": False, "reason": f"필드 누락({field})", "risks": [], "news_ids": []}
    action = str(item.get("action", "")).upper().strip()
    if action not in ["BUY", "WAIT", "SELL"]:
        action = "WAIT"
    return {
        "symbol": symbol,
        "action": action,
        "bad_news": bool(item.get("bad_news", False)),
        "reason": str(item.get("reason", "")),
        "risks": item.get("risks", []) if isinstance(item.get("risks"), list) else [],
        "news_ids": item.get("news_ids", []) if isinstance(item.get("news_ids"), list) else []
    }

def run() -> None:
    log.info("news_judge.run: 실행 시작")
    config = get_config()
    llm_cfg = config.get("llm", {})
    model_name = llm_cfg.get("model")
    max_retries = llm_cfg.get("max_retries", 1)
    max_headlines = llm_cfg.get("max_headlines_per_stock", 5)
    dc = config.get("disclosure") or {}
    max_disclosures = dc.get("max_per_stock", 5) if dc.get("enabled", False) else 0   # 0 이면 프롬프트에 공시를 넣지 않음
    api_key = env("GEMINI_API_KEY")

    if not api_key:
        log.error("news_judge.run: GEMINI_API_KEY가 없어 실행을 중단합니다.")
        return
    if not model_name:
        log.error("news_judge.run: config.yaml 의 llm.model 이 비어 있어 실행을 중단합니다.")
        return

    run_at = get_current_kst_iso()
    run_date = run_at[:10]

    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT symbol FROM positions_meta")
        held_symbols: Set[str] = set(row[0] for row in cursor.fetchall() if row[0])

        cursor.execute("SELECT symbol, name, momentum_rank, ret_5d, above_ma20 FROM candidates WHERE run_date = ?", (run_date,))
        candidates = cursor.fetchall()

        if not candidates and not held_symbols:
            log.info("news_judge.run: 분석 대상이 없습니다.")
            return

        target_dict: Dict[str, Dict[str, Any]] = {}
        for row in candidates:
            target_dict[row[0]] = {
                "name": row[1], "rank": row[2], "ret_5d": row[3],
                "above_ma20": bool(row[4]), "is_held": (row[0] in held_symbols)
            }
        for sym in held_symbols:
            if sym not in target_dict:
                target_dict[sym] = {"name": sym, "rank": None, "ret_5d": None, "above_ma20": None, "is_held": True}

        stocks_payload = []
        for symbol, info in target_dict.items():
            cursor.execute("""
                SELECT news_id, title, published_at FROM news
                WHERE symbol = ? AND published_at <= ?
                ORDER BY published_at DESC LIMIT ?
            """, (symbol, run_at, max_headlines))
            news_rows = cursor.fetchall()
            stocks_payload.append({
                "symbol": symbol, "name": info["name"], "is_held": info["is_held"],
                "ret_5d": info["ret_5d"], "above_ma20": info["above_ma20"],
                "news": [{"news_id": r[0], "title": r[1], "published_at": r[2]} for r in news_rows],
                "disclosures": _recent_disclosures(cursor, symbol, run_at, max_disclosures),
            })

    prompt = f"기준 시각: {run_at}\n\n아래 대상 종목들의 상태와 최근 뉴스를 분석하여 매수·관망·매도를 판단하십시오:\n\n"
    for s in stocks_payload:
        held_label = "【현재 보유 중】" if s["is_held"] else "【미보유 후보】"
        ret_str = f"{s['ret_5d'] * 100:+.2f}%" if s["ret_5d"] is not None else "정보 없음"
        ma_str = "정보 없음" if s["above_ma20"] is None else ("20일선 위" if s["above_ma20"] else "20일선 아래")
        prompt += f"■ {held_label} {s['name']} ({s['symbol']})\n  - 5일 수익률: {ret_str}, 추세: {ma_str}\n  - 최근 뉴스:\n"
        if s["news"]:
            for n in s["news"]:
                prompt += f"    * [{n['news_id']}] {n['title']} ({n['published_at']})\n"
        else:
            prompt += "    * (관련 뉴스 없음)\n"
        if max_disclosures:
            prompt += "  - 최근 공시(DART):\n"
            for d in s["disclosures"]:
                prompt += f"    * [D:{d['rcept_no']}] {d['report_nm']} ({d['rcept_dt']})\n"
            if not s["disclosures"]:
                prompt += "    * (최근 공시 없음)\n"
        prompt += "\n"

    prompt += "[출력 형식]\nJSON 리스트로만 반환하십시오:\n[\n  {\n    \"symbol\": \"종목코드\", \"action\": \"BUY | WAIT | SELL\", \"bad_news\": true 또는 false, \"reason\": \"근거\", \"risks\": [], \"news_ids\": []\n  }\n]\n"

    parsed_results = {}
    for attempt in range(max_retries + 1):          # 오류 시 llm.max_retries 번 재시도 후 전부 WAIT
        try:
            call_res = call_gemini_api(prompt, model_name, api_key)
            log.info(f"news_judge.run: 토큰 사용량 - 입력: {call_res['prompt_tokens']}, 출력: {call_res['candidates_tokens']}")
            raw_list = json.loads(call_res["text"])
            if isinstance(raw_list, list):
                for item in raw_list:
                    if isinstance(item, dict) and "symbol" in item:
                        parsed_results[item["symbol"]] = item
            break
        except Exception as e:
            log.error(f"news_judge.run: LLM 호출 에러 ({type(e).__name__}: {e}) -> "
                      f"{'재시도' if attempt < max_retries else 'Fallback(WAIT) 적용'}")

    final_decisions = []
    for s in stocks_payload:
        sym = s["symbol"]
        item = parsed_results.get(sym)
        final_decisions.append(validate_decision_item(item, sym))

    with connect() as conn:
        cursor = conn.cursor()
        for d in final_decisions:
            cursor.execute("""
                INSERT INTO decisions (run_at, symbol, action, bad_news, reason, risks, news_ids, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (run_at, d["symbol"], d["action"], 1 if d["bad_news"] else 0,
                  d["reason"], json.dumps(d["risks"], ensure_ascii=False),
                  json.dumps(d["news_ids"], ensure_ascii=False), STATUS_NEW))
    log.info(f"news_judge.run: {len(final_decisions)}건 저장 완료")
