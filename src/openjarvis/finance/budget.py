"""Source-aware finance helpers for budgets and market observations.

This module performs arithmetic and validation only. It does not place orders or
pretend to provide personalized investment advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BudgetLine:
    category: str
    description: str
    quantity: Decimal
    unit_cost: Decimal
    currency: str = "USD"

    @property
    def total(self) -> Decimal:
        if self.quantity < 0 or self.unit_cost < 0:
            raise ValueError("quantity and unit_cost cannot be negative")
        return self.quantity * self.unit_cost


@dataclass(frozen=True, slots=True)
class MarketObservation:
    symbol: str
    value: Decimal
    currency: str
    source: str
    as_of: datetime
    metric: str = "price"

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.source.strip():
            raise ValueError("symbol and source are required")
        if self.value < 0:
            raise ValueError("market observation value cannot be negative")


def calculate_budget(lines: list[BudgetLine], *, contingency_percent: Decimal = Decimal("0")) -> dict[str, object]:
    if contingency_percent < 0 or contingency_percent > 100:
        raise ValueError("contingency_percent must be between 0 and 100")
    currencies = {line.currency.upper() for line in lines}
    if len(currencies) > 1:
        raise ValueError("convert currencies before calculating a single total")
    subtotal = sum((line.total for line in lines), Decimal("0"))
    contingency = subtotal * contingency_percent / Decimal("100")
    return {
        "currency": next(iter(currencies), "USD"),
        "subtotal": subtotal,
        "contingency": contingency,
        "total": subtotal + contingency,
        "line_count": len(lines),
        "basis": "quantity × unit_cost; contingency applied to subtotal",
    }


__all__ = ["BudgetLine", "MarketObservation", "calculate_budget"]
