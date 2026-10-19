# AQuA 모의투자 API 정리

실습 사이트의 'API 문서' 페이지를 팀용으로 정리한 것입니다. 원문과 다르면 원문이 기준입니다.
코드에서는 이 API를 직접 부르지 말고 `common/aqua_client.py`의 함수를 씁니다.

- 모든 응답은 JSON, 모든 시각은 ISO-8601 (KST, `+09:00`)
- 로그인 이후 모든 요청 헤더: `Authorization: Bearer {accessToken}` (리더보드 제외)
- 서버 주소와 팀 코드는 `.env`의 `AQUA_BASE_URL`, `AQUA_TEAM_CODE`

## 규칙

| 항목 | 내용 |
| --- | --- |
| 초기 자금 | 1억 원 |
| 거래 횟수 | 매 시간 30회, 매시 정각 초기화. 주문 1건 = 1회. 거절된 주문, 틱 체결, 취소는 소모하지 않음 |
| 같은 종목 재거래 | 주문을 넣는 순간(체결 여부와 무관) 그 종목은 30분간 매수·매도 불가. 틱에서 체결되면 30분을 다시 셈. 취소는 언제든 가능 |
| 호출 빈도 | 팀 단위 분당 100회. 초과 시 429 `RATE_LIMIT_EXCEEDED`(재로그인해도 그대로) |
| 거래 시간 | 09:00 ~ 15:00 |
| 거래 종목 | 코스피 상장 보통주 |
| 수수료·세금 | 수수료 체결 금액의 0.015%(매수·매도), 매도 세금 0.20%. 정산마다 1원 미만 절사. 왕복 약 0.23% |

## 체결 방식

- 실제 호가창 기준. 매수는 매도호가를 낮은 가격부터, 매도는 매수호가를 높은 가격부터 잔량만큼 체결. 각 호가는 걸려 있던 가격 그대로 체결.
- **시장가**(`price` 생략): 지금 잔량만큼 즉시 체결하고 남는 수량은 버림(`CANCELLED`). 닿는 호가가 없으면 422 `NO_LIQUIDITY`.
- **지정가**(`price` 지정): 매수는 `price` 이하, 매도는 `price` 이상 호가만 체결. 남는 수량은 `OPEN`/`PARTIAL`로 대기하고 서버가 15분 틱마다 재시도. 취소할 때까지 유효.
- 대기 중인 매수 주문은 `price × 미체결 수량 + 수수료`만큼 현금을, 매도 주문은 미체결 수량만큼 주식을 묶어 둠.
- 매수는 `체결 금액 + 수수료`가 현금에서 빠지고, 매도는 `체결 금액 − 수수료 − 세금`이 들어옴.

## 엔드포인트

| 기능 | 요청 | 응답 핵심 | `aqua_client` 함수 |
| --- | --- | --- | --- |
| 로그인 | `POST /api/auth/login` `{teamCode}` | `accessToken`(24시간), `expiresAt`, `teamId`, `teamName` | `login()` |
| 로그인 확인 | `GET /api/auth/me` | `teamId`, `teamName` | `me()` |
| 종목 목록·검색 | `GET /api/stocks?keyword=` | `stocks:[{symbol, name}]` (생략 시 전체) | `list_stocks()` |
| 현재가 | `GET /api/stocks/price?symbols=a,b` | `result:[{symbol, price, timestamp}]`, 최대 200개 | `get_prices()` |
| 호가 | `GET /api/stocks/{symbol}/orderbook` | `asks`(낮은 가격부터), `bids`(높은 가격부터): `[{price, volume}]` | `get_orderbook()` |
| 캔들 | `GET /api/stocks/{symbol}/candles?interval=1m\|1d&count=&before=` | `candles:[{timestamp, openPrice, highPrice, lowPrice, closePrice, volume}]`, `nextBefore` | `get_candles()` |
| 주문 | `POST /api/orders` `{symbol, order_code: BUY\|SELL, quantity, price?}` | 아래 '주문 응답' | `place_order()` (팀장 전용) |
| 주문 취소 | `DELETE /api/orders/{orderId}` | 주문 한 건(`status: CANCELLED`) | `cancel_order()` (팀장 전용) |
| 거래 제한 현황 | `GET /api/orders/limit` | `remainingTrades`, `resetAt`, `cooldowns:[{symbol, tradableAt}]` | `get_limit()` |
| 주문 내역 | `GET /api/orders?page=&size=&status=` | `orders:[...]`, `totalPages` (size 최대 100) | `get_orders()` |
| 포트폴리오 | `GET /api/portfolio` | `cash`, `availableCash`, `totalAssetValue`, `totalProfit`, `holdings` | `get_portfolio()` |
| 리더보드 | `GET /api/leaderboard` (토큰 불필요) | `baseline`(KODEX 200, 069500), `rankings`, 15분마다 갱신 | `get_leaderboard()` |

**캔들 이어받기:** 한 번에 최대 200개. 더 과거는 직전 응답의 `nextBefore`를 `before`로 넘깁니다. `+09:00`은 URL 인코딩이 필요합니다(`requests`의 `params=`로 넘기면 자동 처리).

**주문 응답**

| 필드 | 설명 |
| --- | --- |
| `orderId`, `status` | `OPEN` 대기 · `PARTIAL` 일부 체결 · `FILLED` 전량 체결 · `CANCELLED` 취소(시장가 미체결 잔량 포함) |
| `quantity`, `filledQuantity` | 주문 수량, 체결된 수량 |
| `fills` | 호가별 체결 내역 `[{price, quantity, executedAt}]`. 틱 체결은 뒤에 추가 |
| `executedPrice` | 체결 수량 가중 평균 단가(반올림). 체결 없으면 `null` |
| `totalAmount` | 체결 금액 합계(수수료·세금 미포함) |
| `fee`, `tax` | 지금까지 낸 수수료, 매도 세금 |
| `remainCash`, `availableCash` | 현금 잔액, 지금 새 주문에 쓸 수 있는 현금 |
| `remainingTrades` | 이번 시간 남은 거래 횟수 |

**포트폴리오:** `totalAssetValue` = 현금 + 보유 주식 평가금액(현재가 기준). `totalProfit` = `totalAssetValue` − 1억(수수료·세금 반영됨). 새 주문은 `availableCash`, `availableQuantity` 안에서만 가능.

## 에러

모든 에러는 `{"error": {"code": "...", "message": "..."}}` 형태입니다.

| HTTP | code | 의미 | 우리 코드의 처리 |
| --- | --- | --- | --- |
| 400 | `INVALID_REQUEST` | 파라미터 누락·오류 | 버그. 로그 남기기 |
| 401 | `UNAUTHORIZED` | 토큰 없음·만료·무효 | 재로그인 후 1회 재시도 |
| 401 | `INVALID_TEAM_CODE` | 팀 코드 없음 | `.env` 확인 |
| 404 | `UNKNOWN_SYMBOL` | 코스피 보통주가 아님 | 해당 요청 SKIPPED |
| 404 | `TEAM_NOT_FOUND` | 없는 팀 | `.env` 확인 |
| 404 | `ORDER_NOT_FOUND` | 내 주문이 아니거나 없는 번호 | 로그 |
| 422 | `MARKET_CLOSED` | 장 시간 외 | 주문하지 않음 |
| 422 | `INSUFFICIENT_CASH` | 현금 부족(대기 주문 제외 기준) | SKIPPED |
| 422 | `INSUFFICIENT_QUANTITY` | 매도 가능 수량 부족 | SKIPPED |
| 422 | `NO_LIQUIDITY` | 시장가인데 닿는 호가 없음 | SKIPPED (다음 사이클 재판단) |
| 422 | `ORDER_NOT_OPEN` | 이미 체결·취소된 주문 취소 | 로그 |
| 429 | `TRADE_LIMIT_EXCEEDED` | 이번 시간 거래 횟수 소진 | 요청 NEW 유지, `resetAt`까지 주문 중단 |
| 429 | `SYMBOL_COOLDOWN` | 30분 락. 메시지에 `tradable again at ...` | 요청 NEW 유지, 락 풀린 뒤 재처리 |
| 429 | `RATE_LIMIT_EXCEEDED` | 분당 호출 초과 | `Retry-After` 헤더만큼 쉬고 재시도 |
| 502 | `MARKET_DATA_UNAVAILABLE` | 시세 제공처 장애 | 잠시 후 1회 재시도 |

## 아직 확인할 것

- 현재가·호가가 실시간으로 바뀌는지, 15분 틱 단위로 바뀌는지 (데이터 담당이 오픈 후 1분 간격 조회로 확인)
- 장 시작 전(08시대)에 조회 API가 응답하는지 (10/20 확인)
