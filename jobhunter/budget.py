from dataclasses import dataclass

from .config import AppConfig
from .database import Database


@dataclass
class CostEstimate:
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class BudgetGate:
    def __init__(self, config: AppConfig, database: Database):
        self.config = config
        self.database = database

    def estimate(self, input_text: str, max_output_tokens: int) -> CostEstimate:
        input_tokens = estimate_tokens(input_text)
        output_tokens = max_output_tokens
        cost = (
            input_tokens * self.config.cost.input_usd_per_million
            + output_tokens * self.config.cost.output_usd_per_million
        ) / 1_000_000
        return CostEstimate(input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost_usd=cost)

    def can_spend(self, estimate: CostEstimate) -> bool:
        if self.database.spend_today() + estimate.estimated_cost_usd > self.config.cost.daily_budget_usd:
            return False
        if self.database.spend_this_month() + estimate.estimated_cost_usd > self.config.cost.monthly_budget_usd:
            return False
        return True

    def budget_exceeded_reason(self, estimate: CostEstimate) -> str:
        if self.database.spend_today() + estimate.estimated_cost_usd > self.config.cost.daily_budget_usd:
            return "daily"
        if self.database.spend_this_month() + estimate.estimated_cost_usd > self.config.cost.monthly_budget_usd:
            return "monthly"
        return ""

    def record(
        self,
        task: str,
        model: str,
        estimate: CostEstimate,
        actual_output_text: str,
        actual_input_tokens: int = None,
        actual_output_tokens: int = None,
    ) -> None:
        input_tokens = actual_input_tokens if actual_input_tokens is not None else estimate.input_tokens
        output_tokens = (
            actual_output_tokens
            if actual_output_tokens is not None
            else estimate_tokens(actual_output_text)
            if actual_output_text
            else estimate.output_tokens
        )
        cost = (
            input_tokens * self.config.cost.input_usd_per_million
            + output_tokens * self.config.cost.output_usd_per_million
        ) / 1_000_000
        self.database.log_usage(task, model, input_tokens, output_tokens, cost)


def estimate_tokens(text: str) -> int:
    # Cheap, conservative approximation for budget gating.
    return max(1, int(len(text or "") / 4))
