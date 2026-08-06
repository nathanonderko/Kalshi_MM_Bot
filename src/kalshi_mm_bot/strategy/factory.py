from __future__ import annotations

from collections.abc import Mapping

from kalshi_mm_bot.strategy.adaptive import AdaptivePredictionMarketMakerStrategy
from kalshi_mm_bot.strategy.dumb import DumbMarketMakerStrategy
from kalshi_mm_bot.strategy.types import Strategy

STRATEGY_NAMES: tuple[str, ...] = ("adaptive", "dumb")


def strategy_from_name(
    name: str,
    *,
    count: int,
    max_position: int,
    adaptive_params: Mapping[str, int] | None = None,
) -> Strategy:
    normalized = name.strip().lower()

    if normalized in {"adaptive", "prediction", "adaptive_prediction_mm"}:
        params = dict(adaptive_params or ())
        params.setdefault("count", count)
        params.setdefault("max_position", max_position)
        return AdaptivePredictionMarketMakerStrategy(
            **params,
        )

    if normalized in {"dumb", "benchmark", "dumb_join_top"}:
        return DumbMarketMakerStrategy(
            count=count,
            max_position=max_position,
        )

    raise ValueError(f"unknown strategy: {name!r}")
