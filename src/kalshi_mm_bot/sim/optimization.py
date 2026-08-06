from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Literal

from kalshi_mm_bot.market.price import (
    COUNT_SCALE,
    format_count_fp,
    format_price_fp,
)
from kalshi_mm_bot.sim.accounting import format_contract_count, format_money_value
from kalshi_mm_bot.sim.backtest import BacktestResult, BacktestSummary, run_replay_backtest
from kalshi_mm_bot.sim.fills import FillModel
from kalshi_mm_bot.strategy import format_adaptive_params, strategy_from_name
from kalshi_mm_bot.strategy.requote import RequotePolicy

OptimizationObjective = Literal[
    "mark_to_market",
    "cash",
    "volume",
    "fills",
]
ProgressCallback = Callable[["OptimizationTrial"], None]
StopRequested = Callable[[], bool]
ExecutionSearchValue = int | float

EXECUTION_PARAMETER_NAMES = (
    "order_size",
    "max_position",
    "min_requote_sec",
    "min_order_rest_sec",
    "requote_price_threshold",
    "requote_size_threshold_bps",
)

DEFAULT_ADAPTIVE_SEARCH_SPACE: dict[str, tuple[int, ...]] = {
    "min_profit_edge": (15, 25, 40),
    "liquidity_fraction_bps": (2_500, 5_000, 7_500),
    "inventory_skew": (200, 300, 500),
    "adverse_move_threshold": (50, 100),
}
DEFAULT_MAX_OPTIMIZATION_TRIALS = 250


@dataclass(frozen=True, slots=True)
class OptimizationSettings:
    count: int
    max_position: int
    requote_policy: RequotePolicy
    starting_balance_cents: int | None = None


@dataclass(frozen=True, slots=True)
class OptimizationTrial:
    index: int
    total: int
    params: dict[str, int]
    settings: OptimizationSettings
    result: BacktestResult
    objective: OptimizationObjective
    objective_value: int


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    trials: tuple[OptimizationTrial, ...]
    objective: OptimizationObjective

    @property
    def best_trial(self) -> OptimizationTrial:
        if not self.trials:
            raise ValueError("optimization produced no trials")

        return max(
            self.trials,
            key=lambda trial: _trial_sort_key(
                trial.result.summary,
                trial.objective_value,
            ),
        )


async def optimize_adaptive_backtest(
    recording: str | Path,
    *,
    count: int,
    max_position: int,
    fill_model_factory: Callable[[], FillModel],
    fixed_params: Mapping[str, int] | None = None,
    search_space: Mapping[str, Iterable[int]] | None = None,
    execution_search_space: Mapping[str, Iterable[ExecutionSearchValue]] | None = None,
    optimize_execution: bool = False,
    objective: OptimizationObjective = "mark_to_market",
    speed_multiplier: float = 0.0,
    latency_seconds: float = 0.0,
    requote_policy: RequotePolicy | None = None,
    starting_balance_cents: int | None = None,
    max_trials: int | None = DEFAULT_MAX_OPTIMIZATION_TRIALS,
    on_progress: ProgressCallback | None = None,
    stop_requested: StopRequested | None = None,
) -> OptimizationResult:
    fixed = dict(fixed_params or ())
    grid = _normalized_search_space(search_space or DEFAULT_ADAPTIVE_SEARCH_SPACE, fixed)
    policy = requote_policy or RequotePolicy()
    execution_grid = (
        _normalized_execution_search_space(
            execution_search_space
            or default_execution_search_space(count, max_position, policy, starting_balance_cents)
        )
        if optimize_execution
        else {}
    )
    combinations = _limited_combinations(
        _combined_combinations(grid, execution_grid, count, max_position, starting_balance_cents),
        max_trials,
    )
    trials: list[OptimizationTrial] = []

    for index, (params, execution_params) in enumerate(combinations, start=1):
        if stop_requested is not None and stop_requested():
            break

        trial_params = {**fixed, **params}
        settings = _optimization_settings(
            count=count,
            max_position=max_position,
            requote_policy=policy,
            execution_params=execution_params,
            starting_balance_cents=starting_balance_cents,
        )
        strategy = strategy_from_name(
            "adaptive",
            count=settings.count,
            max_position=settings.max_position,
            adaptive_params=trial_params,
        )
        result = await run_replay_backtest(
            recording,
            strategy=strategy,
            fill_model=fill_model_factory(),
            speed_multiplier=speed_multiplier,
            latency_seconds=latency_seconds,
            requote_policy=settings.requote_policy,
            starting_balance_cents=settings.starting_balance_cents,
        )
        trial = OptimizationTrial(
            index=index,
            total=len(combinations),
            params=trial_params,
            settings=settings,
            result=result,
            objective=objective,
            objective_value=_objective_value(result.summary, objective),
        )
        trials.append(trial)

        if on_progress is not None:
            on_progress(trial)

        await asyncio.sleep(0)

    if not trials:
        raise RuntimeError("optimization stopped before any trials completed")

    return OptimizationResult(trials=tuple(trials), objective=objective)


def format_optimization_trial(trial: OptimizationTrial) -> str:
    summary = trial.result.summary
    return (
        f"{trial.index}/{trial.total} "
        f"{trial.objective}={_format_objective_value(trial)} "
        f"mtm={format_money_value(summary.mark_to_market_value)} "
        f"volume={format_contract_count(summary.volume_count)} "
        f"fills={summary.fill_count} "
        f"settings={format_optimization_settings(trial.settings)} "
        f"params={format_adaptive_params(trial.params)}"
    )


def format_optimization_settings(settings: OptimizationSettings) -> str:
    policy = settings.requote_policy
    parts = [
        f"order_size={format_count_fp(settings.count)}",
        f"max_position={format_count_fp(settings.max_position)}",
        f"min_requote_sec={_format_seconds(policy.min_requote_seconds)}",
        f"min_order_rest_sec={_format_seconds(policy.min_order_rest_seconds)}",
        f"requote_price_threshold={format_price_fp(policy.price_change_threshold)}",
        f"requote_size_threshold_bps={policy.size_change_threshold_bps}",
    ]

    if settings.starting_balance_cents is not None:
        parts.append(f"balance={_format_cents(settings.starting_balance_cents)}")

    return ", ".join(parts)


def default_execution_search_space(
    count: int,
    max_position: int,
    requote_policy: RequotePolicy,
    starting_balance_cents: int | None,
) -> dict[str, tuple[ExecutionSearchValue, ...]]:
    return {
        "order_size": _default_order_size_candidates(count, starting_balance_cents),
        "max_position": _default_max_position_candidates(max_position, starting_balance_cents),
        "min_requote_sec": (requote_policy.min_requote_seconds,),
        "min_order_rest_sec": _unique_float_candidates(
            requote_policy.min_order_rest_seconds,
            0.0,
            0.25,
            0.5,
        ),
        "requote_price_threshold": _unique_int_candidates(
            requote_policy.price_change_threshold,
            0,
            100,
            200,
        ),
        "requote_size_threshold_bps": _unique_int_candidates(
            requote_policy.size_change_threshold_bps,
            0,
            2_500,
            5_000,
        ),
    }


def _normalized_search_space(
    search_space: Mapping[str, Iterable[int]],
    fixed_params: Mapping[str, int],
) -> dict[str, tuple[int, ...]]:
    grid: dict[str, tuple[int, ...]] = {}

    for name, raw_values in search_space.items():
        if name in fixed_params:
            continue

        values = tuple(dict.fromkeys(raw_values))

        if not values:
            raise ValueError(f"optimizer parameter {name!r} has no candidate values")

        grid[name] = values

    return grid


def _normalized_execution_search_space(
    search_space: Mapping[str, Iterable[ExecutionSearchValue]],
) -> dict[str, tuple[ExecutionSearchValue, ...]]:
    grid: dict[str, tuple[ExecutionSearchValue, ...]] = {}

    for name, raw_values in search_space.items():
        if name not in EXECUTION_PARAMETER_NAMES:
            valid = ", ".join(EXECUTION_PARAMETER_NAMES)
            raise ValueError(
                f"unknown execution optimizer parameter {name!r}; valid names: {valid}"
            )

        values = tuple(dict.fromkeys(raw_values))

        if not values:
            raise ValueError(f"optimizer parameter {name!r} has no candidate values")

        grid[name] = values

    return grid


def _parameter_combinations(
    search_space: Mapping[str, tuple[ExecutionSearchValue, ...]],
) -> tuple[dict[str, ExecutionSearchValue], ...]:
    if not search_space:
        return ({},)

    names = tuple(search_space)
    return tuple(
        dict(zip(names, values, strict=True))
        for values in product(*(search_space[name] for name in names))
    )


def _combined_combinations(
    adaptive_grid: Mapping[str, tuple[int, ...]],
    execution_grid: Mapping[str, tuple[ExecutionSearchValue, ...]],
    count: int,
    max_position: int,
    starting_balance_cents: int | None,
) -> tuple[tuple[dict[str, int], dict[str, ExecutionSearchValue]], ...]:
    adaptive_combos = _parameter_combinations(adaptive_grid)
    execution_combos = _parameter_combinations(execution_grid)
    combos: list[tuple[dict[str, int], dict[str, ExecutionSearchValue]]] = []

    for execution_params in execution_combos:
        settings = _optimization_settings(
            count=count,
            max_position=max_position,
            requote_policy=RequotePolicy(),
            execution_params=execution_params,
            starting_balance_cents=starting_balance_cents,
        )

        if not _valid_execution_settings(settings):
            continue

        for adaptive_params in adaptive_combos:
            combos.append((dict(adaptive_params), dict(execution_params)))

    if not combos:
        raise ValueError("optimizer grid has no valid candidate combinations")

    return tuple(combos)


def _limited_combinations(
    combinations: tuple[tuple[dict[str, int], dict[str, ExecutionSearchValue]], ...],
    max_trials: int | None,
) -> tuple[tuple[dict[str, int], dict[str, ExecutionSearchValue]], ...]:
    if max_trials is None or len(combinations) <= max_trials:
        return combinations

    if max_trials <= 0:
        raise ValueError("max_trials must be greater than zero")

    if max_trials == 1:
        return (combinations[0],)

    indexes = {
        round(index * (len(combinations) - 1) / (max_trials - 1))
        for index in range(max_trials)
    }
    return tuple(combinations[index] for index in sorted(indexes))


def _optimization_settings(
    *,
    count: int,
    max_position: int,
    requote_policy: RequotePolicy,
    execution_params: Mapping[str, ExecutionSearchValue],
    starting_balance_cents: int | None,
) -> OptimizationSettings:
    return OptimizationSettings(
        count=int(execution_params.get("order_size", count)),
        max_position=int(execution_params.get("max_position", max_position)),
        requote_policy=RequotePolicy(
            min_requote_seconds=float(
                execution_params.get("min_requote_sec", requote_policy.min_requote_seconds)
            ),
            min_order_rest_seconds=float(
                execution_params.get("min_order_rest_sec", requote_policy.min_order_rest_seconds)
            ),
            price_change_threshold=int(
                execution_params.get(
                    "requote_price_threshold",
                    requote_policy.price_change_threshold,
                )
            ),
            size_change_threshold_bps=int(
                execution_params.get(
                    "requote_size_threshold_bps",
                    requote_policy.size_change_threshold_bps,
                )
            ),
        ),
        starting_balance_cents=starting_balance_cents,
    )


def _valid_execution_settings(settings: OptimizationSettings) -> bool:
    if settings.count <= 0 or settings.max_position < 0:
        return False

    if settings.count > settings.max_position:
        return False

    if settings.starting_balance_cents is not None:
        return settings.max_position <= settings.starting_balance_cents

    return True


def _objective_value(summary: BacktestSummary, objective: OptimizationObjective) -> int:
    if objective == "mark_to_market":
        return summary.mark_to_market_value
    if objective == "cash":
        return summary.cash_value
    if objective == "volume":
        return summary.volume_count
    if objective == "fills":
        return summary.fill_count

    raise ValueError(f"unknown optimization objective: {objective!r}")


def _trial_sort_key(summary: BacktestSummary, objective_value: int) -> tuple[int, int, int]:
    return objective_value, summary.volume_count, -abs(summary.position_count)


def _format_objective_value(trial: OptimizationTrial) -> str:
    if trial.objective in {"mark_to_market", "cash"}:
        return format_money_value(trial.objective_value)

    if trial.objective == "volume":
        return format_contract_count(trial.objective_value)

    return str(trial.objective_value)


def _default_order_size_candidates(
    count: int,
    starting_balance_cents: int | None,
) -> tuple[int, ...]:
    if starting_balance_cents is None:
        return _unique_int_candidates(max(1, count // 2), count, count * 2)

    return _bounded_count_candidates(
        starting_balance_cents,
        starting_balance_cents * 50 // 10_000,
        starting_balance_cents * 100 // 10_000,
        starting_balance_cents * 200 // 10_000,
    )


def _default_max_position_candidates(
    max_position: int,
    starting_balance_cents: int | None,
) -> tuple[int, ...]:
    if starting_balance_cents is None:
        return _unique_int_candidates(
            max(COUNT_SCALE, max_position // 2),
            max_position,
            max_position * 2,
        )

    return _bounded_count_candidates(
        starting_balance_cents,
        starting_balance_cents * 500 // 10_000,
        starting_balance_cents * 1_000 // 10_000,
        starting_balance_cents * 2_000 // 10_000,
    )


def _bounded_count_candidates(limit: int, *values: int) -> tuple[int, ...]:
    return _unique_int_candidates(*(max(1, min(limit, value)) for value in values))


def _unique_int_candidates(*values: int) -> tuple[int, ...]:
    return tuple(dict.fromkeys(value for value in values if value >= 0))


def _unique_float_candidates(*values: float) -> tuple[float, ...]:
    return tuple(dict.fromkeys(value for value in values if value >= 0))


def _format_seconds(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"
