import pytest

from openjarvis.operations.budget import BudgetPlanner


def test_budget_summary_and_scenario():
    budget = BudgetPlanner(currency="USD")
    budget.add("server", "100.50", category="infra", assumption="monthly")
    budget.add("voice", 20, category="api", assumption="usage estimate")
    assert budget.summary()["totals"] == {"USD": "120.50"}
    assert budget.summary()["by_category"]["infra"] == "100.50"
    assert budget.scenario("1.5")["totals"] == {"USD": "180.750"}


def test_budget_rejects_invalid_amounts():
    budget = BudgetPlanner()
    with pytest.raises(ValueError):
        budget.add("bad", -1)
    with pytest.raises(ValueError):
        budget.add("bad", "not-a-number")
    with pytest.raises(ValueError):
        budget.scenario(101)
