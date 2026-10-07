from __future__ import annotations

"""Shared categorical agreement statistics (observed, chance-expected, Cohen kappa)."""

import random
from dataclasses import dataclass, field

# Kappa's percentile bootstrap resamples the paired items with a fixed seed, so the same labels
# always give the same interval.
KAPPA_BOOTSTRAP_RESAMPLES = 2000
KAPPA_BOOTSTRAP_SEED = 20261007


@dataclass(frozen=True)
class CategoricalAgreement:
    confusion: dict[str, dict[str, int]]
    observed_agreement: float | None
    expected_agreement: float | None
    cohen_kappa: float | None
    # Proportion of specific agreement per category, 2*n_ii / (row_i + col_i). With a rare class
    # (few "relevant" items) kappa and raw agreement hide how often the raters agree on it.
    specific_agreement: dict[str, float | None] = field(default_factory=dict)
    # 95% percentile bootstrap interval for kappa; None when kappa is undefined.
    kappa_bootstrap_95: tuple[float, float] | None = None


def empty_confusion(labels: tuple[str, ...]) -> dict[str, dict[str, int]]:
    return {first: {second: 0 for second in labels} for first in labels}


def _kappa(confusion: dict[str, dict[str, int]], paired: int) -> tuple[float, float, float | None]:
    labels = tuple(confusion)
    observed = sum(confusion[label][label] for label in labels) / paired
    expected = sum(
        sum(confusion[label].values()) * sum(confusion[first][label] for first in labels)
        for label in labels
    ) / (paired * paired)
    return observed, expected, (observed - expected) / (1 - expected) if expected < 1 else None


def _kappa_bootstrap(confusion: dict[str, dict[str, int]], paired: int) -> tuple[float, float] | None:
    pairs = [(first, second) for first, row in confusion.items()
             for second, count in row.items() for _ in range(count)]
    labels = tuple(confusion)
    rng = random.Random(KAPPA_BOOTSTRAP_SEED)
    values = []
    for _ in range(KAPPA_BOOTSTRAP_RESAMPLES):
        sample = empty_confusion(labels)
        for first, second in rng.choices(pairs, k=len(pairs)):
            sample[first][second] += 1
        kappa = _kappa(sample, paired)[2]
        if kappa is not None:
            values.append(kappa)
    if not values:
        return None
    values.sort()
    return values[int(0.025 * (len(values) - 1))], values[int(0.975 * (len(values) - 1))]


def categorical_agreement(confusion: dict[str, dict[str, int]], paired: int) -> CategoricalAgreement:
    """Kappa is undefined (None) when both raters use one identical class: never claim perfection."""
    if not paired:
        return CategoricalAgreement(confusion, None, None, None)
    observed, expected, kappa = _kappa(confusion, paired)
    labels = tuple(confusion)
    specific = {}
    for label in labels:
        marginal = sum(confusion[label].values()) + sum(confusion[first][label] for first in labels)
        specific[label] = 2 * confusion[label][label] / marginal if marginal else None
    interval = _kappa_bootstrap(confusion, paired) if kappa is not None else None
    return CategoricalAgreement(confusion, observed, expected, kappa, specific, interval)
