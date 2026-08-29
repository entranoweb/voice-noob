"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { CheckCircle, XCircle, Clock, AlertCircle, ExternalLink, TrendingUp } from "lucide-react";
import type { TestRun } from "@/lib/api/qa";
import { cn } from "@/lib/utils";

// =============================================================================
// Types
// =============================================================================

interface TestResultsSummaryProps {
  testRun: TestRun;
  onViewDetails?: (evaluationId: string) => void;
  className?: string;
}

interface ScenarioResultDetail {
  scenario_id: string;
  scenario_name: string;
  passed: boolean;
  score: number;
  failure_reason?: string;
  duration_ms?: number;
  evaluation_id?: string;
}

// =============================================================================
// Component
// =============================================================================

export function TestResultsSummary({ testRun, onViewDetails, className }: TestResultsSummaryProps) {
  const passRate = testRun.pass_rate ?? 0;
  const passRatePercent = passRate * 100;

  // Parse results from test run
  const results: ScenarioResultDetail[] = (testRun.results ?? []).map((r) => ({
    scenario_id: r.scenario_id as string,
    scenario_name: (r.scenario_name as string) ?? "Unknown Scenario",
    passed: r.passed as boolean,
    score: (r.score as number) ?? 0,
    failure_reason: r.failure_reason as string | undefined,
    duration_ms: r.duration_ms as number | undefined,
    evaluation_id: r.evaluation_id as string | undefined,
  }));

  const avgScore =
    results.length > 0 ? results.reduce((sum, r) => sum + r.score, 0) / results.length : 0;

  const totalDuration = results.reduce((sum, r) => sum + (r.duration_ms ?? 0), 0);

  const getScoreColor = (score: number) => {
    if (score >= 80) return "text-green-600";
    if (score >= 60) return "text-yellow-600";
    return "text-red-600";
  };

  const getScoreBgColor = (score: number) => {
    if (score >= 80) return "bg-green-100 text-green-800";
    if (score >= 60) return "bg-yellow-100 text-yellow-800";
    return "bg-red-100 text-red-800";
  };

  return (
    <Card className={cn("", className)}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-medium">Test Results Summary</CardTitle>
          <Badge
            variant={testRun.status === "completed" ? "default" : "secondary"}
            className="capitalize"
          >
            {testRun.status}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Summary Stats */}
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div className="text-center">
            <div className="flex items-center justify-center gap-1 text-green-600">
              <CheckCircle className="h-4 w-4" />
              <span className="text-2xl font-bold">{testRun.passed_scenarios}</span>
            </div>
            <p className="text-xs text-muted-foreground">Passed</p>
          </div>
          <div className="text-center">
            <div className="flex items-center justify-center gap-1 text-red-600">
              <XCircle className="h-4 w-4" />
              <span className="text-2xl font-bold">{testRun.failed_scenarios}</span>
            </div>
            <p className="text-xs text-muted-foreground">Failed</p>
          </div>
          <div className="text-center">
            <div className="flex items-center justify-center gap-1">
              <TrendingUp className="h-4 w-4 text-muted-foreground" />
              <span className={cn("text-2xl font-bold", getScoreColor(avgScore))}>
                {avgScore.toFixed(0)}
              </span>
            </div>
            <p className="text-xs text-muted-foreground">Avg Score</p>
          </div>
          <div className="text-center">
            <div className="flex items-center justify-center gap-1">
              <Clock className="h-4 w-4 text-muted-foreground" />
              <span className="text-2xl font-bold">{(totalDuration / 1000).toFixed(1)}s</span>
            </div>
            <p className="text-xs text-muted-foreground">Duration</p>
          </div>
        </div>

        {/* Pass Rate Progress */}
        <div>
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-medium">Pass Rate</span>
            <span className={cn("text-sm font-bold", getScoreColor(passRatePercent))}>
              {passRatePercent.toFixed(1)}%
            </span>
          </div>
          <Progress
            value={passRatePercent}
            className={cn(
              "h-2",
              passRatePercent >= 80 && "[&>div]:bg-green-500",
              passRatePercent >= 60 && passRatePercent < 80 && "[&>div]:bg-yellow-500",
              passRatePercent < 60 && "[&>div]:bg-red-500"
            )}
          />
        </div>

        {/* Individual Results */}
        {results.length > 0 && (
          <div>
            <h4 className="mb-2 text-sm font-medium">Scenario Results</h4>
            <ScrollArea className="max-h-64">
              <div className="space-y-2">
                {results.map((result) => (
                  <ScenarioResultRow
                    key={result.scenario_id}
                    result={result}
                    getScoreBgColor={getScoreBgColor}
                    onViewDetails={onViewDetails}
                  />
                ))}
              </div>
            </ScrollArea>
          </div>
        )}

        {/* Timestamps */}
        <div className="flex items-center justify-between border-t pt-2 text-xs text-muted-foreground">
          <span>
            Started: {testRun.started_at ? new Date(testRun.started_at).toLocaleString() : "N/A"}
          </span>
          <span>
            Completed:{" "}
            {testRun.completed_at ? new Date(testRun.completed_at).toLocaleString() : "N/A"}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

// =============================================================================
// Sub-components
// =============================================================================

interface ScenarioResultRowProps {
  result: ScenarioResultDetail;
  getScoreBgColor: (score: number) => string;
  onViewDetails?: (evaluationId: string) => void;
}

function ScenarioResultRow({ result, getScoreBgColor, onViewDetails }: ScenarioResultRowProps) {
  return (
    <div
      className={cn(
        "flex items-center gap-3 rounded-md border p-2",
        result.passed ? "border-green-200 bg-green-50/50" : "border-red-200 bg-red-50/50"
      )}
    >
      {result.passed ? (
        <CheckCircle className="h-4 w-4 shrink-0 text-green-600" />
      ) : (
        <XCircle className="h-4 w-4 shrink-0 text-red-600" />
      )}

      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">{result.scenario_name}</p>
        {result.failure_reason && (
          <p className="flex items-center gap-1 truncate text-xs text-red-600">
            <AlertCircle className="h-3 w-3" />
            {result.failure_reason}
          </p>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <Badge variant="secondary" className={getScoreBgColor(result.score)}>
          {result.score}
        </Badge>
        {result.duration_ms && (
          <span className="text-xs text-muted-foreground">
            {(result.duration_ms / 1000).toFixed(1)}s
          </span>
        )}
        {result.evaluation_id && onViewDetails && (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 w-6 p-0"
            onClick={() => onViewDetails(result.evaluation_id ?? "")}
          >
            <ExternalLink className="h-3 w-3" />
          </Button>
        )}
      </div>
    </div>
  );
}

export default TestResultsSummary;
