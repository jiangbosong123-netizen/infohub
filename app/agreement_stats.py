from __future__ import annotations

"""Shared categorical agreement statistics (observed, chance-expected, Cohen kappa)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoricalAgreement:
    confusion: dict[str, dict[str, int]]
    observed_agreement: float | None
    expected_agreement: float | None
    cohen_kappa: float | None


def empty_confusion(labels: tuple[str, ...]) -> dict[str, dict[str, int]]:
    return {first: {second: 0 for second in labels} for first in labels}


def categorical_agreement(confusion: dict[str, dict[str, int]], paired: int) -> CategoricalAgreement:
    """Kappa is undefined (None) when both raters use one identical class: never claim perfection."""
    if not paired:
        return CategoricalAgreement(confusion, None, None, None)
    labels = tuple(confusion)
    observed = sum(confusion[label][label] for label in labels) / paired
    expected = sum(
        sum(confusion[label].values()) * sum(confusion[first][label] for first in labels)
        for label in labels
    ) / (paired * paired)
    kappa = (observed - expected) / (1 - expected) if expected < 1 else None
    return CategoricalAgreement(confusion, observed, expected, kappa)
