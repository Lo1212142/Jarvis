"""Project budgeting and cost planning; informational, not transaction execution."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True, slots=True)
class BudgetLine:
    name: str
    amount: Decimal
    currency: str
    category: str
    assumption: str = ""


class BudgetPlanner:
    def __init__(self, *, currency: str = "USD") -> None:
        self.currency = currency.upper()[:8]
        self.lines: list[BudgetLine] = []
        self.audit: list[dict[str, Any]] = []

    def add(self, name: str, amount: str | int | float | Decimal, *, category: str = "uncategorized", currency: str | None = None, assumption: str = "") -> BudgetLine:
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("amount must be numeric") from exc
        if value < 0 or value > Decimal("1000000000000"):
            raise ValueError("amount is outside the supported non-negative range")
        line = BudgetLine(name[:200], value, (currency or self.currency).upper()[:8], category[:64], assumption[:500])
        self.lines.append(line)
        self.audit.append({"event": "line_added", "name": line.name, "category": line.category})
        return line

    def summary(self) -> dict[str, Any]:
        totals: dict[str, Decimal] = {}
        by_category: dict[str, Decimal] = {}
        for line in self.lines:
            totals[line.currency] = totals.get(line.currency, Decimal("0")) + line.amount
            by_category[line.category] = by_category.get(line.category, Decimal("0")) + line.amount
        return {"totals": {key: str(value) for key, value in totals.items()}, "by_category": {key: str(value) for key, value in by_category.items()}, "line_count": len(self.lines)}

    def scenario(self, multiplier: str | int | float | Decimal) -> dict[str, Any]:
        try:
            factor = Decimal(str(multiplier))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("scenario multiplier must be numeric") from exc
        if factor < 0 or factor > 100:
            raise ValueError("scenario multiplier must be between 0 and 100")
        return {"multiplier": str(factor), "totals": {key: str(value * factor) for key, value in self._totals().items()}, "assumptions": [line.assumption for line in self.lines if line.assumption]}

    def _totals(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for line in self.lines:
            totals[line.currency] = totals.get(line.currency, Decimal("0")) + line.amount
        return totals


__all__ = ["BudgetLine", "BudgetPlanner"]
