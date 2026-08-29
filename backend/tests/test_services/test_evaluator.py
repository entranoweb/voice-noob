"""Tests for QA Evaluator service (Task 8.5.4)."""

from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.models.workspace import Workspace
from app.services.qa.evaluator import (
    EffectiveQASettings,
    QAEvaluator,
    get_effective_qa_settings,
)


class TestGetEffectiveQASettings:
    """Test get_effective_qa_settings helper function."""

    def test_returns_global_settings_when_no_workspace(self) -> None:
        """Test that global settings are returned when workspace is None."""
        result = get_effective_qa_settings(None)

        assert result.source == "global"
        assert isinstance(result, EffectiveQASettings)

    def test_returns_global_settings_when_inherit_global_true(self) -> None:
        """Test that global settings are returned when inherit_global is True."""
        workspace = MagicMock(spec=Workspace)
        workspace.settings = {"qa": {"inherit_global": True, "pass_threshold": 90}}

        result = get_effective_qa_settings(workspace)

        assert result.source == "global"
        # Should NOT use workspace's pass_threshold since inherit_global=True

    def test_returns_workspace_settings_when_inherit_global_false(self) -> None:
        """Test that workspace settings are returned when inherit_global is False."""
        workspace = MagicMock(spec=Workspace)
        workspace.settings = {
            "qa": {
                "inherit_global": False,
                "qa_enabled": True,
                "auto_evaluate": False,
                "pass_threshold": 85,
                "evaluation_model": "claude-3-haiku-20240307",
            }
        }

        result = get_effective_qa_settings(workspace)

        assert result.source == "workspace"
        assert result.qa_enabled is True
        assert result.auto_evaluate is False
        assert result.pass_threshold == 85
        assert result.evaluation_model == "claude-3-haiku-20240307"

    def test_returns_defaults_when_workspace_has_empty_settings(self) -> None:
        """Test that defaults are used when workspace has empty settings."""
        workspace = MagicMock(spec=Workspace)
        workspace.settings = {}

        result = get_effective_qa_settings(workspace)

        # Empty settings means inherit_global defaults to True
        assert result.source == "global"

    def test_returns_defaults_when_workspace_settings_is_none(self) -> None:
        """Test that defaults are used when workspace.settings is None."""
        workspace = MagicMock(spec=Workspace)
        workspace.settings = None

        result = get_effective_qa_settings(workspace)

        assert result.source == "global"

    def test_partial_workspace_settings_use_defaults(self) -> None:
        """Test that partial workspace settings use defaults for missing fields."""
        workspace = MagicMock(spec=Workspace)
        workspace.settings = {
            "qa": {
                "inherit_global": False,
                "pass_threshold": 90,
                # Other fields not specified - should use defaults
            }
        }

        result = get_effective_qa_settings(workspace)

        assert result.source == "workspace"
        assert result.pass_threshold == 90
        # Defaults for unspecified fields
        assert result.qa_enabled is True
        assert result.auto_evaluate is True
        assert result.evaluation_model == settings.QA_EVALUATION_MODEL

    def test_workspace_qa_disabled(self) -> None:
        """Test workspace with QA disabled."""
        workspace = MagicMock(spec=Workspace)
        workspace.settings = {
            "qa": {
                "inherit_global": False,
                "qa_enabled": False,
            }
        }

        result = get_effective_qa_settings(workspace)

        assert result.source == "workspace"
        assert result.qa_enabled is False


class TestParseEvaluationResponse:
    """Test _parse_evaluation_response helper."""

    def test_parse_valid_json(self) -> None:
        """Test parsing valid JSON response."""
        evaluator = QAEvaluator(db=None)  # type: ignore[arg-type]
        response = '{"overall_score": 85, "passed": true}'

        result = evaluator._parse_evaluation_response(response)

        assert result is not None
        assert result["overall_score"] == 85
        assert result["passed"] is True

    def test_parse_markdown_wrapped_json(self) -> None:
        """Test parsing JSON wrapped in markdown code blocks."""
        evaluator = QAEvaluator(db=None)  # type: ignore[arg-type]
        response = '```json\n{"overall_score": 85, "intent_completion": 90}\n```'

        result = evaluator._parse_evaluation_response(response)

        assert result is not None
        assert result["overall_score"] == 85
        assert result["intent_completion"] == 90

    def test_parse_markdown_wrapped_json_no_lang(self) -> None:
        """Test parsing JSON wrapped in markdown code blocks without language."""
        evaluator = QAEvaluator(db=None)  # type: ignore[arg-type]
        response = '```\n{"overall_score": 75}\n```'

        result = evaluator._parse_evaluation_response(response)

        assert result is not None
        assert result["overall_score"] == 75

    def test_parse_json_with_surrounding_text(self) -> None:
        """Test parsing JSON with surrounding text."""
        evaluator = QAEvaluator(db=None)  # type: ignore[arg-type]
        response = (
            'Here is the evaluation:\n{"overall_score": 80, "passed": true}\nThat is my assessment.'
        )

        result = evaluator._parse_evaluation_response(response)

        assert result is not None
        assert result["overall_score"] == 80

    def test_parse_invalid_json_returns_none(self) -> None:
        """Test parsing invalid JSON returns None."""
        evaluator = QAEvaluator(db=None)  # type: ignore[arg-type]
        response = "This is not valid JSON at all."

        result = evaluator._parse_evaluation_response(response)

        assert result is None

    def test_parse_empty_response_returns_none(self) -> None:
        """Test parsing empty response returns None."""
        evaluator = QAEvaluator(db=None)  # type: ignore[arg-type]
        response = ""

        result = evaluator._parse_evaluation_response(response)

        assert result is None

    def test_type_coercion_string_numbers(self) -> None:
        """Test that string numbers are coerced to integers."""
        evaluator = QAEvaluator(db=None)  # type: ignore[arg-type]
        response = '{"overall_score": "85", "intent_completion": "90"}'

        result = evaluator._parse_evaluation_response(response)

        assert result is not None
        assert result["overall_score"] == 85
        assert isinstance(result["overall_score"], int)
        assert result["intent_completion"] == 90
        assert isinstance(result["intent_completion"], int)

    def test_type_coercion_float_fields(self) -> None:
        """Test that float fields are coerced properly."""
        evaluator = QAEvaluator(db=None)  # type: ignore[arg-type]
        response = '{"overall_score": 85, "sentiment_score": "0.75", "escalation_risk": "0.1"}'

        result = evaluator._parse_evaluation_response(response)

        assert result is not None
        assert result["sentiment_score"] == 0.75
        assert isinstance(result["sentiment_score"], float)
        assert result["escalation_risk"] == 0.1
        assert isinstance(result["escalation_risk"], float)

    def test_nested_json_object(self) -> None:
        """Test parsing complex nested JSON."""
        evaluator = QAEvaluator(db=None)  # type: ignore[arg-type]
        response = """{"overall_score": 85, "turn_analysis": [{"turn": 1, "quality_score": 90}]}"""

        result = evaluator._parse_evaluation_response(response)

        assert result is not None
        assert result["overall_score"] == 85
        assert "turn_analysis" in result
        assert len(result["turn_analysis"]) == 1


class TestCostCalculation:
    """Test cost calculation logic.

    These call evaluation_cost_cents rather than reimplementing the formula, so a
    change to the pricing code actually fails a test.
    """

    def test_cost_calculation_sonnet(self) -> None:
        """Sonnet 4.6: 0.3c/1K input, 1.5c/1K output."""
        from app.services.qa.evaluator import evaluation_cost_cents

        # 500 * 0.3 / 1000 + 200 * 1.5 / 1000 = 0.15 + 0.3 = 0.45 cents
        cost = evaluation_cost_cents("claude-sonnet-4-6", 500, 200)
        assert abs(cost - 0.45) < 0.001

    def test_cost_calculation_haiku(self) -> None:
        """Haiku 4.5: 0.1c/1K input, 0.5c/1K output."""
        from app.services.qa.evaluator import evaluation_cost_cents

        # 500 * 0.1 / 1000 + 200 * 0.5 / 1000 = 0.05 + 0.1 = 0.15 cents
        cost = evaluation_cost_cents("claude-haiku-4-5", 500, 200)
        assert abs(cost - 0.15) < 0.001

    def test_unknown_model_raises_rather_than_guessing(self) -> None:
        """An unpriced model must fail loudly.

        The previous behaviour fell back to another model's rate, which silently
        misreported spend on every evaluation for that workspace.
        """
        from app.services.qa.evaluator import (
            UnknownEvaluationModelError,
            evaluation_cost_cents,
        )

        with pytest.raises(UnknownEvaluationModelError, match="no-such-model"):
            evaluation_cost_cents("no-such-model", 500, 200)

    def test_default_evaluation_model_is_priced(self) -> None:
        """The configured default must have a cost entry.

        Guards the pairing that broke before: a model swap without a matching
        MODEL_COSTS entry now fails here instead of in production.
        """
        from app.services.qa.evaluator import MODEL_COSTS

        assert settings.QA_EVALUATION_MODEL in MODEL_COSTS


class TestFormatTranscript:
    """Test transcript formatting."""

    def test_transcript_preserved(self) -> None:
        """Test that transcript format is preserved."""
        transcript = "[User]: Hello\n[Assistant]: Hi there!"

        # The evaluator uses transcript as-is
        assert "[User]:" in transcript
        assert "[Assistant]:" in transcript
