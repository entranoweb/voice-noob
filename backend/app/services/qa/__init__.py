"""QA Testing Framework services."""

from app.services.qa.alerts import (
    acknowledge_alert,
    check_failure_spike_alert,
    check_score_drop_alert,
    create_alert,
    get_alerts,
    send_failure_alert,
)
from app.services.qa.dashboard import (
    get_agent_comparison,
    get_dashboard_metrics,
    get_top_failure_reasons,
    get_trends,
)
from app.services.qa.evaluator import QAEvaluator, trigger_qa_evaluation
from app.services.qa.resilience import (
    call_claude_with_resilience,
    get_anthropic_client,
    get_circuit_state,
    is_circuit_open,
    reset_circuit_breaker,
)
from app.services.qa.scenarios import (
    get_built_in_scenarios,
    get_scenarios_by_category,
    get_scenarios_by_difficulty,
)
from app.services.qa.test_caller import AITestCaller, TestResult
from app.services.qa.test_runner import TestRunner, seed_scenarios_background

__all__ = [
    "AITestCaller",
    "QAEvaluator",
    "TestResult",
    "TestRunner",
    "acknowledge_alert",
    "call_claude_with_resilience",
    "check_failure_spike_alert",
    "check_score_drop_alert",
    "create_alert",
    "get_agent_comparison",
    "get_alerts",
    "get_anthropic_client",
    "get_built_in_scenarios",
    "get_circuit_state",
    "get_dashboard_metrics",
    "get_scenarios_by_category",
    "get_scenarios_by_difficulty",
    "get_top_failure_reasons",
    "get_trends",
    "is_circuit_open",
    "reset_circuit_breaker",
    "seed_scenarios_background",
    "send_failure_alert",
    "trigger_qa_evaluation",
]
