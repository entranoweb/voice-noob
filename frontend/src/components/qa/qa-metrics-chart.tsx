"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { TrendData } from "@/lib/api/qa";

interface ScoreBreakdown {
  intent_completion: number;
  tool_usage: number;
  compliance: number;
  response_quality: number;
}

interface QAMetricsChartProps {
  trendData?: TrendData;
  scoreBreakdown?: ScoreBreakdown;
  title?: string;
}

export function QAMetricsChart({
  trendData,
  scoreBreakdown,
  title = "Score Trends",
}: QAMetricsChartProps) {
  const getBarColor = (score: number) => {
    if (score >= 80) return "bg-green-500";
    if (score >= 60) return "bg-yellow-500";
    return "bg-red-500";
  };

  return (
    <div className="space-y-4">
      {/* Trend Chart */}
      {trendData && trendData.dates.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">{title}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex h-32 items-end gap-1">
              {trendData.values.map((value, index) => (
                <div
                  key={index}
                  className="flex-1 cursor-pointer rounded-t bg-primary/20 transition-colors hover:bg-primary/40"
                  style={{
                    height: `${Math.max((value / 100) * 100, 5)}%`,
                  }}
                  title={`${trendData.dates[index]}: ${value}`}
                />
              ))}
            </div>
            <div className="mt-2 flex justify-between text-xs text-muted-foreground">
              <span>{trendData.dates[0]}</span>
              <span>{trendData.dates[trendData.dates.length - 1]}</span>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Score Breakdown Bar Chart */}
      {scoreBreakdown && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Score Breakdown</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="w-28 text-sm text-muted-foreground">Intent</span>
                <div className="h-4 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className={`h-full ${getBarColor(scoreBreakdown.intent_completion)} transition-all`}
                    style={{ width: `${scoreBreakdown.intent_completion}%` }}
                  />
                </div>
                <span className="w-8 text-right text-sm font-medium">
                  {Math.round(scoreBreakdown.intent_completion)}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <span className="w-28 text-sm text-muted-foreground">Tool Usage</span>
                <div className="h-4 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className={`h-full ${getBarColor(scoreBreakdown.tool_usage)} transition-all`}
                    style={{ width: `${scoreBreakdown.tool_usage}%` }}
                  />
                </div>
                <span className="w-8 text-right text-sm font-medium">
                  {Math.round(scoreBreakdown.tool_usage)}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <span className="w-28 text-sm text-muted-foreground">Compliance</span>
                <div className="h-4 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className={`h-full ${getBarColor(scoreBreakdown.compliance)} transition-all`}
                    style={{ width: `${scoreBreakdown.compliance}%` }}
                  />
                </div>
                <span className="w-8 text-right text-sm font-medium">
                  {Math.round(scoreBreakdown.compliance)}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <span className="w-28 text-sm text-muted-foreground">Quality</span>
                <div className="h-4 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className={`h-full ${getBarColor(scoreBreakdown.response_quality)} transition-all`}
                    style={{ width: `${scoreBreakdown.response_quality}%` }}
                  />
                </div>
                <span className="w-8 text-right text-sm font-medium">
                  {Math.round(scoreBreakdown.response_quality)}
                </span>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
