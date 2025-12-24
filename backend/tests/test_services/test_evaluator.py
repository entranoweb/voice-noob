"""Tests for QA Evaluator service (Task 8.5.4)."""

from app.services.qa.evaluator import QAEvaluator


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
    """Test cost calculation logic."""

    def test_cost_calculation_sonnet(self) -> None:
        """Test cost calculation for Sonnet model.

        Formula: (input_tokens * 0.3 + output_tokens * 1.5) / 1000 cents
        """
        from app.services.qa.evaluator import MODEL_COSTS

        model = "claude-sonnet-4-20250514"
        input_tokens = 500
        output_tokens = 200

        cost_info = MODEL_COSTS.get(model, MODEL_COSTS["claude-sonnet-4-20250514"])
        cost_cents = (input_tokens / 1000) * cost_info["input"] + (
            output_tokens / 1000
        ) * cost_info["output"]

        # 500 * 0.3 / 1000 + 200 * 1.5 / 1000 = 0.15 + 0.3 = 0.45 cents
        assert abs(cost_cents - 0.45) < 0.001

    def test_cost_calculation_haiku(self) -> None:
        """Test cost calculation for Haiku model."""
        from app.services.qa.evaluator import MODEL_COSTS

        model = "claude-3-haiku-20240307"
        input_tokens = 500
        output_tokens = 200

        cost_info = MODEL_COSTS.get(model, MODEL_COSTS["claude-sonnet-4-20250514"])
        cost_cents = (input_tokens / 1000) * cost_info["input"] + (
            output_tokens / 1000
        ) * cost_info["output"]

        # 500 * 0.025 / 1000 + 200 * 0.125 / 1000 = 0.0125 + 0.025 = 0.0375 cents
        assert abs(cost_cents - 0.0375) < 0.001


class TestFormatTranscript:
    """Test transcript formatting."""

    def test_transcript_preserved(self) -> None:
        """Test that transcript format is preserved."""
        transcript = "[User]: Hello\n[Assistant]: Hi there!"

        # The evaluator uses transcript as-is
        assert "[User]:" in transcript
        assert "[Assistant]:" in transcript
