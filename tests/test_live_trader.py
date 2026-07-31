import asyncio

import pytest

from kalshi_mm_bot.live import LiveOrderManager, LivePortfolio
from kalshi_mm_bot.live.trader import _cancel_on_shutdown
from kalshi_mm_bot.market.price import COUNT_SCALE, parse_price_fp
from kalshi_mm_bot.market.types import MarketPosition, OrderFill
from kalshi_mm_bot.strategy.types import QuoteIntent


class FakeRest:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.canceled: list[dict] = []
        self.create_response: dict | None = None
        self.reverse_create_response = False
        self.cancel_failures: set[str] = set()
        self.resting_orders_by_ticker: dict[str, list[dict]] = {}
        self.available_balance_cents = 1_000_000

    async def batch_create_orders(self, orders) -> dict:
        payloads = [order.to_json() for order in orders]
        self.created.extend(payloads)

        if self.create_response is not None:
            return self.create_response

        response_orders = [
            {
                "order_id": f"live-{index}",
                "client_order_id": payload["client_order_id"],
            }
            for index, payload in enumerate(payloads, start=1)
        ]

        if self.reverse_create_response:
            response_orders.reverse()

        return {"orders": response_orders}

    async def batch_cancel_orders(self, orders) -> dict:
        payloads = [order.to_json() for order in orders]
        self.canceled.extend(payloads)

        for payload in payloads:
            if payload["order_id"] in self.cancel_failures:
                raise RuntimeError(f"cancel failed for {payload['order_id']}")

        return {}

    async def get_available_balance_cents(self) -> int:
        return self.available_balance_cents

    async def get_orders(self, **kwargs) -> list[dict]:
        return list(self.resting_orders_by_ticker.get(kwargs.get("ticker"), ()))


def buy_intent(
    price: str = "0.5000",
    count: int = COUNT_SCALE,
    quote_id: str = "M1:adaptive:yes:buy",
) -> QuoteIntent:
    return QuoteIntent(
        quote_id=quote_id,
        market_ticker="M1",
        action="buy",
        side="yes",
        yes_price=parse_price_fp(price),
        count=count,
    )


def test_live_order_manager_dry_run_replaces_changed_quotes() -> None:
    async def run() -> None:
        manager = LiveOrderManager(FakeRest(), dry_run=True, min_requote_seconds=0)

        created, canceled = await manager.sync_quotes("M1", [buy_intent()], now=1)
        assert (created, canceled) == (1, 0)
        assert len(manager.orders) == 1

        created, canceled = await manager.sync_quotes("M1", [buy_intent("0.4900")], now=2)
        assert (created, canceled) == (1, 1)
        assert len(manager.orders) == 1

    asyncio.run(run())


def test_live_order_manager_rate_limits_replacement_create_not_stale_cancel() -> None:
    async def run() -> None:
        manager = LiveOrderManager(FakeRest(), dry_run=True, min_requote_seconds=10)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4900")], now=2) == (0, 1)
        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_keeps_matching_quote_inside_requote_interval() -> None:
    async def run() -> None:
        manager = LiveOrderManager(FakeRest(), dry_run=True, min_requote_seconds=10)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent()], now=2) == (0, 0)
        assert len(manager.orders) == 1

    asyncio.run(run())


def test_live_order_manager_creates_replacement_after_requote_interval() -> None:
    async def run() -> None:
        manager = LiveOrderManager(FakeRest(), dry_run=True, min_requote_seconds=10)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4900")], now=2) == (0, 1)
        assert await manager.sync_quotes("M1", [buy_intent("0.4900")], now=11) == (1, 0)

    asyncio.run(run())


def test_live_order_manager_rejects_empty_client_prefix() -> None:
    with pytest.raises(ValueError, match="client_prefix"):
        LiveOrderManager(FakeRest(), client_prefix="-")


def test_live_order_manager_rejects_duplicate_quote_ids_before_canceling() -> None:
    async def run() -> None:
        rest = FakeRest()
        manager = LiveOrderManager(rest, dry_run=True, min_requote_seconds=0)

        await manager.sync_quotes("M1", [buy_intent()], now=1)

        with pytest.raises(ValueError, match="duplicate quote_id"):
            await manager.sync_quotes("M1", [buy_intent("0.4900"), buy_intent("0.4800")], now=2)

        assert len(manager.orders) == 1
        assert rest.canceled == []

    asyncio.run(run())


def test_live_order_manager_counts_confirmed_real_creates_only() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {"orders": []}
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        with pytest.raises(RuntimeError, match="batch create returned"):
            await manager.sync_quotes("M1", [buy_intent()], now=1)

        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_skips_real_create_when_balance_is_exhausted() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.available_balance_cents = 0
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (0, 0)
        assert rest.created == []
        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_skips_real_create_when_estimated_cost_exceeds_balance() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.available_balance_cents = 49
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        assert await manager.sync_quotes("M1", [buy_intent("0.5000")], now=1) == (0, 0)
        assert rest.created == []
        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_logs_rejected_create_entry_without_order_id() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {
            "orders": [
                {
                    "code": "invalid order",
                    "details": "post only cross",
                }
            ]
        }
        logs: list[str] = []
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0, status=logs.append)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (0, 0)
        assert manager.orders == {}
        assert any("post only cross" in line for line in logs)

    asyncio.run(run())


def test_live_order_manager_cools_down_rejected_quote_before_retrying() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {
            "orders": [
                {
                    "code": "invalid_order",
                    "details": "post only cross",
                }
            ]
        }
        manager = LiveOrderManager(
            rest,
            dry_run=False,
            min_requote_seconds=0,
            rejection_cooldown_seconds=1.0,
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1.0) == (0, 0)
        assert len(rest.created) == 1

        assert await manager.sync_quotes("M1", [buy_intent()], now=1.5) == (0, 0)
        assert len(rest.created) == 1

        assert await manager.sync_quotes("M1", [buy_intent()], now=2.1) == (0, 0)
        assert len(rest.created) == 2

    asyncio.run(run())


def test_live_order_manager_tracks_created_entries_when_other_create_entries_reject() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {
            "orders": [
                {
                    "code": "invalid order",
                    "details": "post only cross",
                },
                {
                    "order_id": "live-2",
                    "remaining_count": "1.00",
                },
            ]
        }
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        created, canceled = await manager.sync_quotes(
            "M1",
            [buy_intent(), buy_intent("0.4900", quote_id="M1:adaptive:yes:buy-2")],
            now=1,
        )

        assert (created, canceled) == (1, 0)
        assert [order.order_id for order in manager.orders.values()] == ["live-2"]

    asyncio.run(run())


def test_live_order_manager_updates_remaining_count_from_user_orders() -> None:
    async def run() -> None:
        manager = LiveOrderManager(FakeRest(), dry_run=True, min_requote_seconds=0)

        await manager.sync_quotes("M1", [buy_intent()], now=1)
        old_client_order_id = next(iter(manager.orders))

        manager.handle_user_order(
            {
                "type": "user_order",
                "msg": {
                    "order_id": manager.orders[old_client_order_id].order_id,
                    "client_order_id": old_client_order_id,
                    "status": "resting",
                    "remaining_count_fp": "0.50",
                },
            }
        )

        created, canceled = await manager.sync_quotes("M1", [buy_intent()], now=2)

        assert (created, canceled) == (1, 1)
        assert old_client_order_id not in manager.orders

    asyncio.run(run())


def test_live_order_manager_does_not_track_fully_filled_create_response() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {
            "orders": [
                {
                    "order_id": "live-1",
                    "remaining_count": "0.00",
                }
            ]
        }
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_matches_create_response_by_client_order_id() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.reverse_create_response = True
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        await manager.sync_quotes(
            "M1",
            [buy_intent(), buy_intent("0.4900", quote_id="M1:adaptive:yes:buy-2")],
            now=1,
        )

        order_ids_by_quote = {
            order.quote_id: order.order_id
            for order in manager.orders.values()
        }

        assert order_ids_by_quote == {
            "M1:adaptive:yes:buy": "live-1",
            "M1:adaptive:yes:buy-2": "live-2",
        }

    asyncio.run(run())


def test_live_order_manager_cancel_all_attempts_remaining_orders_after_failure() -> None:
    async def run() -> None:
        rest = FakeRest()
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)
        await manager.sync_quotes("M1", [buy_intent(), buy_intent("0.4900", quote_id="M1:adaptive:yes:sell")], now=1)
        first_order_id = next(iter(manager.orders.values())).order_id
        rest.cancel_failures.add(first_order_id)

        with pytest.raises(RuntimeError, match="failed to cancel"):
            await manager.cancel_all()

        assert len(rest.canceled) == 2
        assert len(manager.orders) == 1

    asyncio.run(run())


def test_shutdown_cancel_sweeps_untracked_prefix_orders() -> None:
    async def run() -> None:
        rest = FakeRest()
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)
        await manager.sync_quotes("M1", [buy_intent()], now=1)
        rest.resting_orders_by_ticker["M1"] = [
            {"order_id": "untracked-1", "client_order_id": f"{manager.client_prefix}-old-1"}
        ]

        canceled = await _cancel_on_shutdown(manager, ("M1",), dry_run=False)

        assert canceled == 2
        assert {payload["order_id"] for payload in rest.canceled} == {
            "live-1",
            "untracked-1",
        }

    asyncio.run(run())


def test_shutdown_cancel_does_not_repeat_recently_canceled_orders() -> None:
    async def run() -> None:
        rest = FakeRest()
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)
        await manager.sync_quotes("M1", [buy_intent()], now=1)
        live_order = next(iter(manager.orders.values()))
        rest.resting_orders_by_ticker["M1"] = [
            {
                "order_id": live_order.order_id,
                "client_order_id": live_order.client_order_id,
            }
        ]

        canceled = await _cancel_on_shutdown(manager, ("M1",), dry_run=False)

        assert canceled == 1
        assert [payload["order_id"] for payload in rest.canceled] == [live_order.order_id]

    asyncio.run(run())


def test_live_portfolio_applies_private_position_messages() -> None:
    portfolio = LivePortfolio()
    fill = OrderFill(
        trade_id="t1",
        order_id="o1",
        market_ticker="M1",
        action="buy",
        side="yes",
        yes_price=parse_price_fp("0.5000"),
        count=COUNT_SCALE,
        post_position=COUNT_SCALE,
        is_taker=False,
    )
    position = MarketPosition(
        market_ticker="M1",
        position=2 * COUNT_SCALE,
        position_cost=0,
        realized_pnl=0,
        fees_paid=0,
        volume=0,
    )

    portfolio.apply_message(fill)
    assert portfolio.position("M1") == COUNT_SCALE

    portfolio.apply_message(position)
    assert portfolio.position("M1") == 2 * COUNT_SCALE
