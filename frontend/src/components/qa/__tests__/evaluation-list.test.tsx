/**
 * Tests for EvaluationList component (Task 20.5.4)
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EvaluationList } from "../evaluation-list";

const mockEvaluations = [
  {
    id: "eval-1",
    call_id: "call-1",
    agent_id: "agent-1",
    workspace_id: "workspace-1",
    overall_score: 85,
    intent_completion: 90,
    tool_usage: 80,
    compliance: 95,
    response_quality: 75,
    passed: true,
    coherence: 88,
    relevance: 85,
    groundedness: 90,
    fluency: 82,
    overall_sentiment: "positive",
    sentiment_score: 0.7,
    escalation_risk: 0.1,
    objectives_detected: ["schedule appointment"],
    objectives_completed: ["schedule appointment"],
    failure_reasons: null,
    recommendations: [],
    evaluation_model: "claude-sonnet-4-20250514",
    evaluation_latency_ms: 1500,
    evaluation_cost_cents: 0.3,
    created_at: "2024-01-15T10:00:00Z",
  },
  {
    id: "eval-2",
    call_id: "call-2",
    agent_id: "agent-1",
    workspace_id: "workspace-1",
    overall_score: 55,
    intent_completion: 50,
    tool_usage: 60,
    compliance: 70,
    response_quality: 40,
    passed: false,
    coherence: 55,
    relevance: 50,
    groundedness: 60,
    fluency: 55,
    overall_sentiment: "negative",
    sentiment_score: -0.3,
    escalation_risk: 0.6,
    objectives_detected: ["get order status", "speak to human"],
    objectives_completed: [],
    failure_reasons: ["Intent not completed", "Slow response"],
    recommendations: ["Improve response time", "Add escalation handling"],
    evaluation_model: "claude-sonnet-4-20250514",
    evaluation_latency_ms: 1800,
    evaluation_cost_cents: 0.35,
    created_at: "2024-01-14T10:00:00Z",
  },
];

// Type-safe access to mock evaluations
const passedEvaluation = mockEvaluations.find((e) => e.passed);
const failedEvaluation = mockEvaluations.find((e) => !e.passed);

describe("EvaluationList", () => {
  it("renders pass badge for passed=true", () => {
    if (!passedEvaluation) throw new Error("Test setup error: no passed evaluation");
    render(<EvaluationList evaluations={[passedEvaluation]} />);
    const badge = screen.getByText("Pass");
    expect(badge).toBeInTheDocument();
  });

  it("renders fail badge for passed=false", () => {
    if (!failedEvaluation) throw new Error("Test setup error: no failed evaluation");
    render(<EvaluationList evaluations={[failedEvaluation]} />);
    const badge = screen.getByText("Fail");
    expect(badge).toBeInTheDocument();
  });

  it("displays score values", () => {
    render(<EvaluationList evaluations={mockEvaluations} />);
    // Check that scores are displayed
    expect(screen.getByText("85")).toBeInTheDocument();
    expect(screen.getByText("55")).toBeInTheDocument();
  });

  it("renders empty state when no evaluations", () => {
    render(<EvaluationList evaluations={[]} />);
    expect(screen.getByText(/no evaluations/i)).toBeInTheDocument();
  });

  it("renders table headers", () => {
    render(<EvaluationList evaluations={mockEvaluations} />);
    expect(screen.getByText("Status")).toBeInTheDocument();
    expect(screen.getByText("Score")).toBeInTheDocument();
    expect(screen.getByText("Intent")).toBeInTheDocument();
    expect(screen.getByText("Date")).toBeInTheDocument();
  });

  it("displays individual scores for each category", () => {
    if (!passedEvaluation) throw new Error("Test setup error: no passed evaluation");
    render(<EvaluationList evaluations={[passedEvaluation]} />);
    // Check intent_completion score (90) is displayed
    expect(screen.getByText("90")).toBeInTheDocument();
    // Check tool_usage score (80) is displayed
    expect(screen.getByText("80")).toBeInTheDocument();
    // Check compliance score (95) is displayed
    expect(screen.getByText("95")).toBeInTheDocument();
  });

  it("renders View link for each evaluation when showCallLink is true", () => {
    render(<EvaluationList evaluations={mockEvaluations} showCallLink={true} />);
    const viewLinks = screen.getAllByText("View");
    expect(viewLinks).toHaveLength(2);
  });

  it("hides View link when showCallLink is false", () => {
    render(<EvaluationList evaluations={mockEvaluations} showCallLink={false} />);
    expect(screen.queryByText("View")).not.toBeInTheDocument();
  });
});
