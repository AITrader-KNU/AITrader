"""plan_market_buy: 수수료·호가 잔량을 반영한 시장가 매수 수량 계산"""
from order_manager.manager import plan_market_buy

FEE = 0.00015


def test_single_level_budget_limited():
    qty, cost = plan_market_buy(1_000_000, [{"price": 72_000, "volume": 100}], FEE)
    assert qty == 13                                   # 13 × 72,000 = 936,000 (+수수료 140)
    assert cost + int(cost * FEE) <= 1_000_000


def test_walks_multiple_levels_like_market_order():
    asks = [{"price": 72_000, "volume": 6}, {"price": 72_100, "volume": 4}, {"price": 72_200, "volume": 50}]
    qty, cost = plan_market_buy(800_000, asks, FEE)
    assert qty == 11                                   # 6@72,000 + 4@72,100 + 1@72,200
    assert cost == 6 * 72_000 + 4 * 72_100 + 72_200


def test_limited_by_ask_volume():
    qty, _ = plan_market_buy(10_000_000, [{"price": 50_000, "volume": 3}], FEE)
    assert qty == 3                                    # 잔량보다 많이 사지 않음


def test_cannot_afford_one_share_or_empty_book():
    assert plan_market_buy(50_000, [{"price": 72_000, "volume": 10}], FEE) == (0, 0)
    assert plan_market_buy(1_000_000, [], FEE) == (0, 0)


def test_exact_budget_with_fee():
    # 수수료 때문에 정확히 10주 금액만 있으면 9주만 살 수 있어야 함
    qty, cost = plan_market_buy(720_000, [{"price": 72_000, "volume": 100}], FEE)
    assert qty == 9 and cost + int(cost * FEE) <= 720_000
