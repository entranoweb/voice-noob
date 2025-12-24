"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  CheckCircle,
  XCircle,
  TrendingUp,
  AlertTriangle,
  Activity,
  Target,
  Shield,
  MessageSquare,
} from "lucide-react";
import {
  getDashboardMetrics,
  getTrends,
  getFailureReasons,
  listEvaluations,
  getQAStatus,
} from "@/lib/api/qa";
import { fetchAgents } from "@/lib/api/agents";
import { Skeleton } from "@/components/ui/skeleton";

export default function QADashboardPage() {
  const [days, setDays] = useState<number>(7);
  const [agentId, setAgentId] = useState<string | undefined>(undefined);

  // Check QA status
  const { data: qaStatus } = useQuery({
    queryKey: ["qa-status"],
    queryFn: getQAStatus,
  });

  // Fetch agents for filter
  const { data: agents = [] } = useQuery({
    queryKey: ["agents"],
    queryFn: fetchAgents,
  });

  // Fetch dashboard metrics
  const { data: metrics, isLoading: metricsLoading } = useQuery({
    queryKey: ["qa-metrics", days, agentId],
    queryFn: () => getDashboardMetrics({ days, agent_id: agentId }),
    enabled: qaStatus?.enabled,
  });

  // Fetch trends
  const { data: trends } = useQuery({
    queryKey: ["qa-trends", days, agentId],
    queryFn: () => getTrends({ days, agent_id: agentId, metric: "overall_score" }),
    enabled: qaStatus?.enabled,
  });

  // Fetch failure reasons
  const { data: failureReasons = [] } = useQuery({
    queryKey: ["qa-failure-reasons", days, agentId],
    queryFn: () => getFailureReasons({ days, agent_id: agentId, limit: 5 }),
    enabled: qaStatus?.enabled,
  });

  // Fetch recent evaluations
  const { data: evaluationsData } = useQuery({
    queryKey: ["qa-evaluations", agentId],
    queryFn: () => listEvaluations({ agent_id: agentId, page_size: 10 }),
    enabled: qaStatus?.enabled,
  });

  // If QA is disabled, show a message
  if (qaStatus && !qaStatus.enabled) {
    return (
      <div className="flex min-h-[400px] flex-col items-center justify-center space-y-4">
        <Shield className="h-16 w-16 text-muted-foreground/30" />
        <div className="text-center">
          <h2 className="text-lg font-semibold">QA Testing Disabled</h2>
          <p className="text-sm text-muted-foreground">
            QA evaluation is currently disabled. Contact your administrator to enable it.
          </p>
        </div>
      </div>
    );
  }

  const formatScore = (score: number | undefined) => {
    if (score === undefined || score === null) return "N/A";
    return Math.round(score);
  };

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
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">QA Dashboard</h1>
          <p className="text-sm text-muted-foreground">
            Quality assurance metrics and evaluation insights
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={days.toString()} onValueChange={(v) => setDays(parseInt(v))}>
            <SelectTrigger className="w-[120px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="7">Last 7 days</SelectItem>
              <SelectItem value="14">Last 14 days</SelectItem>
              <SelectItem value="30">Last 30 days</SelectItem>
              <SelectItem value="90">Last 90 days</SelectItem>
            </SelectContent>
          </Select>
          <Select
            value={agentId ?? "all"}
            onValueChange={(v) => setAgentId(v === "all" ? undefined : v)}
          >
            <SelectTrigger className="w-[180px]">
              <SelectValue placeholder="All agents" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All agents</SelectItem>
              {agents.map((agent) => (
                <SelectItem key={agent.id} value={agent.id}>
                  {agent.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="p-4">
            {metricsLoading ? (
              <Skeleton className="h-16 w-full" />
            ) : (
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-xs text-muted-foreground">Pass Rate</p>
                  <p className={`text-2xl font-bold ${getScoreColor((metrics?.pass_rate ?? 0) * 100)}`}>
                    {((metrics?.pass_rate ?? 0) * 100).toFixed(1)}%
                  </p>
                </div>
                <CheckCircle className="h-8 w-8 text-green-500/20" />
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4">
            {metricsLoading ? (
              <Skeleton className="h-16 w-full" />
            ) : (
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-xs text-muted-foreground">Average Score</p>
                  <p className={`text-2xl font-bold ${getScoreColor(metrics?.average_score ?? 0)}`}>
                    {formatScore(metrics?.average_score)}
                  </p>
                </div>
                <TrendingUp className="h-8 w-8 text-blue-500/20" />
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4">
            {metricsLoading ? (
              <Skeleton className="h-16 w-full" />
            ) : (
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-xs text-muted-foreground">Total Evaluations</p>
                  <p className="text-2xl font-bold">{metrics?.total_evaluations ?? 0}</p>
                </div>
                <Activity className="h-8 w-8 text-purple-500/20" />
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4">
            {metricsLoading ? (
              <Skeleton className="h-16 w-full" />
            ) : (
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-xs text-muted-foreground">Failed Calls</p>
                  <p className="text-2xl font-bold text-red-600">{metrics?.failed_count ?? 0}</p>
                </div>
                <XCircle className="h-8 w-8 text-red-500/20" />
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Score Breakdown */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Score Breakdown</CardTitle>
          </CardHeader>
          <CardContent>
            {metricsLoading ? (
              <div className="space-y-4">
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Target className="h-4 w-4 text-muted-foreground" />
                    <span className="text-sm">Intent Completion</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="h-2 w-24 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full bg-blue-500"
                        style={{
                          width: `${metrics?.score_breakdown?.intent_completion ?? 0}%`,
                        }}
                      />
                    </div>
                    <span className="w-8 text-sm font-medium">
                      {formatScore(metrics?.score_breakdown?.intent_completion)}
                    </span>
                  </div>
                </div>

                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Activity className="h-4 w-4 text-muted-foreground" />
                    <span className="text-sm">Tool Usage</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="h-2 w-24 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full bg-green-500"
                        style={{
                          width: `${metrics?.score_breakdown?.tool_usage ?? 0}%`,
                        }}
                      />
                    </div>
                    <span className="w-8 text-sm font-medium">
                      {formatScore(metrics?.score_breakdown?.tool_usage)}
                    </span>
                  </div>
                </div>

                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Shield className="h-4 w-4 text-muted-foreground" />
                    <span className="text-sm">Compliance</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="h-2 w-24 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full bg-purple-500"
                        style={{
                          width: `${metrics?.score_breakdown?.compliance ?? 0}%`,
                        }}
                      />
                    </div>
                    <span className="w-8 text-sm font-medium">
                      {formatScore(metrics?.score_breakdown?.compliance)}
                    </span>
                  </div>
                </div>

                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <MessageSquare className="h-4 w-4 text-muted-foreground" />
                    <span className="text-sm">Response Quality</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="h-2 w-24 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full bg-orange-500"
                        style={{
                          width: `${metrics?.score_breakdown?.response_quality ?? 0}%`,
                        }}
                      />
                    </div>
                    <span className="w-8 text-sm font-medium">
                      {formatScore(metrics?.score_breakdown?.response_quality)}
                    </span>
                  </div>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <AlertTriangle className="h-4 w-4" />
              Top Failure Reasons
            </CardTitle>
          </CardHeader>
          <CardContent>
            {failureReasons.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-8 text-center">
                <CheckCircle className="mb-2 h-8 w-8 text-green-500/30" />
                <p className="text-sm text-muted-foreground">No failures in this period</p>
              </div>
            ) : (
              <div className="space-y-3">
                {failureReasons.map((reason, index) => (
                  <div key={index} className="flex items-center justify-between">
                    <span className="text-sm truncate max-w-[200px]">{reason.reason}</span>
                    <Badge variant="secondary" className="text-xs">
                      {reason.count}
                    </Badge>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Score Trend */}
      {trends && trends.dates.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Score Trend</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-32 flex items-end gap-1">
              {trends.values.map((value, index) => (
                <div
                  key={index}
                  className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors rounded-t"
                  style={{
                    height: `${Math.max((value / 100) * 100, 5)}%`,
                  }}
                  title={`${trends.dates[index]}: ${value}`}
                />
              ))}
            </div>
            <div className="mt-2 flex justify-between text-xs text-muted-foreground">
              <span>{trends.dates[0]}</span>
              <span>{trends.dates[trends.dates.length - 1]}</span>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Recent Evaluations */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium">Recent Evaluations</CardTitle>
        </CardHeader>
        <CardContent>
          {!evaluationsData?.evaluations || evaluationsData.evaluations.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-8 text-center">
              <Activity className="mb-2 h-8 w-8 text-muted-foreground/30" />
              <p className="text-sm text-muted-foreground">No evaluations yet</p>
              <p className="text-xs text-muted-foreground">
                Evaluations will appear here after calls are completed
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {evaluationsData.evaluations.slice(0, 10).map((evaluation) => (
                <div
                  key={evaluation.id}
                  className="flex items-center justify-between rounded-lg border p-3"
                >
                  <div className="flex items-center gap-3">
                    <div
                      className={`flex h-8 w-8 items-center justify-center rounded-md ${
                        evaluation.passed ? "bg-green-100" : "bg-red-100"
                      }`}
                    >
                      {evaluation.passed ? (
                        <CheckCircle className="h-4 w-4 text-green-600" />
                      ) : (
                        <XCircle className="h-4 w-4 text-red-600" />
                      )}
                    </div>
                    <div>
                      <p className="text-sm font-medium">
                        Call {evaluation.call_id.slice(0, 8)}...
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {new Date(evaluation.created_at).toLocaleDateString()}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge className={getScoreBgColor(evaluation.overall_score)}>
                      {evaluation.overall_score}
                    </Badge>
                    <Badge variant={evaluation.passed ? "default" : "destructive"}>
                      {evaluation.passed ? "Pass" : "Fail"}
                    </Badge>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
