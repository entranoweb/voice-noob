/**
 * Tests for QA API Client (Task 20.5.3)
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

// Mock fetch globally
const mockFetch = vi.fn();
global.fetch = mockFetch;

// Mock data
const mockEvaluations = [
  {
    id: "eval-1",
    call_id: "call-1",
    agent_id: "agent-1",
    overall_score: 85,
    intent_completion: 90,
    tool_usage: 80,
    compliance: 95,
    response_quality: 75,
    passed: true,
    created_at: "2024-01-15T10:00:00Z",
  },
  {
    id: "eval-2",
    call_id: "call-2",
    agent_id: "agent-1",
    overall_score: 55,
    passed: false,
    created_at: "2024-01-14T10:00:00Z",
  },
];

const mockDashboardMetrics = {
  total_evaluations: 150,
  passed_count: 123,
  failed_count: 27,
  pass_rate: 0.82,
  average_score: 78.5,
  score_breakdown: {
    intent_completion: 82,
    tool_usage: 75,
    compliance: 88,
    response_quality: 72,
  },
  quality_metrics: {
    coherence: 80,
    relevance: 78,
    groundedness: 82,
    fluency: 76,
  },
  sentiment_distribution: { positive: 90, neutral: 45, negative: 15 },
  latency: { p50_ms: 1200, p90_ms: 2000, p95_ms: 2500 },
  period_days: 7,
};

describe("QA API Client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem("access_token", "test-token");
  });

  describe("listEvaluations", () => {
    it("returns evaluations list response", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () =>
          Promise.resolve({
            evaluations: mockEvaluations,
            total: 2,
            page: 1,
            page_size: 20,
            total_pages: 1,
          }),
      });

      const { listEvaluations } = await import("../api/qa");
      const result = await listEvaluations();

      expect(mockFetch).toHaveBeenCalled();
      expect(result.evaluations).toBeDefined();
      expect(Array.isArray(result.evaluations)).toBe(true);
      expect(result.evaluations[0]).toHaveProperty("overall_score");
      expect(result.evaluations[0]).toHaveProperty("passed");
    });

    it("passes filters to API", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () =>
          Promise.resolve({
            evaluations: mockEvaluations.filter((e) => e.passed),
            total: 1,
            page: 1,
            page_size: 20,
            total_pages: 1,
          }),
      });

      const { listEvaluations } = await import("../api/qa");
      await listEvaluations({ passed: true, page: 1 });

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining("passed=true"),
        expect.any(Object)
      );
    });
  });

  describe("getDashboardMetrics", () => {
    it("returns metrics object", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockDashboardMetrics),
      });

      const { getDashboardMetrics } = await import("../api/qa");
      const result = await getDashboardMetrics({ days: 7 });

      expect(result).toHaveProperty("total_evaluations");
      expect(result).toHaveProperty("pass_rate");
      expect(result).toHaveProperty("average_score");
      expect(result.total_evaluations).toBe(150);
    });
  });

  describe("getEvaluation", () => {
    it("returns single evaluation", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockEvaluations[0]),
      });

      const { getEvaluation } = await import("../api/qa");
      const result = await getEvaluation("eval-1");

      expect(result.id).toBe("eval-1");
      expect(result.overall_score).toBeGreaterThan(0);
    });

    it("handles 404 error", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ detail: "Evaluation not found" }),
      });

      const { getEvaluation } = await import("../api/qa");

      await expect(getEvaluation("nonexistent")).rejects.toThrow();
    });
  });

  describe("getQAStatus", () => {
    it("returns QA status", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () =>
          Promise.resolve({
            enabled: true,
            auto_evaluate: true,
            evaluation_model: "claude-sonnet-4-20250514",
            default_threshold: 70,
            api_key_configured: true,
          }),
      });

      const { getQAStatus } = await import("../api/qa");
      const result = await getQAStatus();

      expect(result).toHaveProperty("enabled");
      expect(result).toHaveProperty("auto_evaluate");
      expect(result.enabled).toBe(true);
    });
  });

  describe("getTrends", () => {
    it("returns trend data", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () =>
          Promise.resolve({
            dates: ["2024-01-10", "2024-01-11", "2024-01-12"],
            values: [75, 78, 82],
            metric: "overall_score",
          }),
      });

      const { getTrends } = await import("../api/qa");
      const result = await getTrends({ days: 7 });

      expect(result).toHaveProperty("dates");
      expect(result).toHaveProperty("values");
      expect(Array.isArray(result.dates)).toBe(true);
      expect(Array.isArray(result.values)).toBe(true);
    });
  });
});
