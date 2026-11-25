"use client";

import { useState, useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Checkbox } from "@/components/ui/checkbox";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Play,
  Loader2,
  CheckCircle,
  XCircle,
  Clock,
  AlertCircle,
  RotateCcw,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import { fetchAgents } from "@/lib/api/agents";
import { listScenarios, runScenario, getTestRun } from "@/lib/api/qa";
import type { TestScenario } from "@/lib/api/qa";
import { cn } from "@/lib/utils";
import { InfoTooltip } from "@/components/ui/info-tooltip";

// =============================================================================
// Types
// =============================================================================

interface TestRunnerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialSelectedScenarios?: string[];
  /** Sent with each run so results are filed under this workspace. */
  workspaceId?: string;
  /** Called once, after the last selected scenario has finished. */
  onTestComplete?: () => void;
}

interface ScenarioResult {
  scenario_id: string;
  scenario_name: string;
  status: "pending" | "running" | "passed" | "failed" | "error";
  score?: number;
  failure_reason?: string;
  duration_ms?: number;
}

type Phase = "idle" | "running" | "done";

/**
 * What a result from this panel does and does not show. Kept as one constant so
 * the wording under the header and on the QA page cannot drift apart.
 */
export const TEXT_HARNESS_NOTICE =
  "These are text simulations. Each scenario sends the agent's prompt and tools to a text model, " +
  "which is not the speech model that answers real calls. A pass here does not cover hearing, " +
  "turn-taking or how the agent behaves on a live call.";

// =============================================================================
// Component
// =============================================================================

export function TestRunner({
  open,
  onOpenChange,
  initialSelectedScenarios = [],
  workspaceId,
  onTestComplete,
}: TestRunnerProps) {
  const [selectedAgentId, setSelectedAgentId] = useState<string>("");
  const [selectedScenarios, setSelectedScenarios] = useState<string[]>(initialSelectedScenarios);
  const [phase, setPhase] = useState<Phase>("idle");
  const [runError, setRunError] = useState<string | null>(null);
  const [scenarioResults, setScenarioResults] = useState<ScenarioResult[]>([]);
  const [expandedResults, setExpandedResults] = useState<Set<string>>(new Set());
  // Bumped whenever a run is abandoned (sheet closed, reset), so a run that is
  // still awaiting the backend stops writing into state that has moved on.
  const runToken = useRef(0);

  // Update selected scenarios when initial selection changes
  useEffect(() => {
    if (initialSelectedScenarios.length > 0) {
      setSelectedScenarios(initialSelectedScenarios);
    }
  }, [initialSelectedScenarios]);

  // Reset state when sheet closes
  useEffect(() => {
    if (!open) {
      runToken.current += 1;
      // Keep results visible for a moment before clearing
      const timer = setTimeout(() => {
        setPhase("idle");
        setRunError(null);
        setScenarioResults([]);
        setExpandedResults(new Set());
      }, 300);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [open]);

  // Fetch agents
  const { data: agents = [] } = useQuery({
    queryKey: ["agents"],
    queryFn: fetchAgents,
  });

  // Fetch scenarios
  const { data: scenarios = [] } = useQuery({
    queryKey: ["qa-scenarios"],
    queryFn: () => listScenarios({}),
  });

  const updateResult = (scenarioId: string, patch: Partial<ScenarioResult>) => {
    setScenarioResults((prev) =>
      prev.map((r) => (r.scenario_id === scenarioId ? { ...r, ...patch } : r))
    );
  };

  /**
   * The backend runs one scenario per request (`POST /api/v1/testing/run`) and
   * answers when that scenario has finished, so the selected scenarios are run
   * one after another and each result is read back from its own run record.
   */
  const runSelected = async (scenarioList: TestScenario[]) => {
    runToken.current += 1;
    const token = runToken.current;
    const stillCurrent = () => runToken.current === token;

    for (const scenario of scenarioList) {
      if (!stillCurrent()) return;
      updateResult(scenario.id, { status: "running" });
      try {
        const started = await runScenario({
          scenario_id: scenario.id,
          agent_id: selectedAgentId,
          workspace_id: workspaceId,
        });
        const run = await getTestRun(started.test_run_id);
        if (!stillCurrent()) return;
        const status: ScenarioResult["status"] =
          run.status === "error" ? "error" : run.passed === true ? "passed" : "failed";
        updateResult(scenario.id, {
          status,
          score: run.overall_score ?? undefined,
          failure_reason:
            status === "passed" ? undefined : (run.error_message ?? run.issues_found?.[0]),
          duration_ms: run.duration_ms ?? undefined,
        });
      } catch (error) {
        if (!stillCurrent()) return;
        // A refused request (testing switched off, no model key, agent gone)
        // would be refused for every remaining scenario too, so stop here and
        // say why instead of marking the rest as failed.
        const message = error instanceof Error ? error.message : "The test could not be run";
        updateResult(scenario.id, { status: "error", failure_reason: message });
        setRunError(message);
        break;
      }
    }

    if (!stillCurrent()) return;
    setPhase("done");
    onTestComplete?.();
  };

  const handleScenarioToggle = (scenarioId: string) => {
    setSelectedScenarios((prev) =>
      prev.includes(scenarioId) ? prev.filter((id) => id !== scenarioId) : [...prev, scenarioId]
    );
  };

  const handleSelectAll = () => {
    if (selectedScenarios.length === scenarios.length) {
      setSelectedScenarios([]);
    } else {
      setSelectedScenarios(scenarios.map((s) => s.id));
    }
  };

  const handleClearSelection = () => {
    setSelectedScenarios([]);
  };

  const handleStartTest = () => {
    if (!selectedAgentId || selectedScenarios.length === 0) return;

    const selectedScenarioList = scenarios.filter((s) => selectedScenarios.includes(s.id));
    setRunError(null);
    setScenarioResults(
      selectedScenarioList.map((s) => ({
        scenario_id: s.id,
        scenario_name: s.name,
        status: "pending",
      }))
    );
    setPhase("running");
    void runSelected(selectedScenarioList);
  };

  const handleReset = () => {
    runToken.current += 1;
    setPhase("idle");
    setRunError(null);
    setScenarioResults([]);
    setExpandedResults(new Set());
  };

  const toggleResultExpanded = (scenarioId: string) => {
    setExpandedResults((prev) => {
      const next = new Set(prev);
      if (next.has(scenarioId)) {
        next.delete(scenarioId);
      } else {
        next.add(scenarioId);
      }
      return next;
    });
  };

  const isRunning = phase === "running";
  const isComplete = phase === "done";

  // Group scenarios by category
  const scenariosByCategory = scenarios.reduce<Record<string, TestScenario[]>>((acc, scenario) => {
    const category = scenario.category ?? "other";
    acc[category] ??= [];
    acc[category].push(scenario);
    return acc;
  }, {});

  // Calculate progress
  const completedCount = scenarioResults.filter(
    (r) => r.status === "passed" || r.status === "failed" || r.status === "error"
  ).length;
  const totalCount = scenarioResults.length || selectedScenarios.length;
  const progressPercent = totalCount > 0 ? (completedCount / totalCount) * 100 : 0;

  // Calculate summary stats
  const passedCount = scenarioResults.filter((r) => r.status === "passed").length;
  const failedCount = scenarioResults.filter((r) => r.status === "failed").length;
  const errorCount = scenarioResults.filter((r) => r.status === "error").length;
  const runningCount = scenarioResults.filter((r) => r.status === "running").length;
  // Null when nothing was scored, so an unscored run does not read as "Avg: 0".
  const scored = scenarioResults.filter((r) => r.score !== undefined);
  const avgScore =
    scored.length > 0 ? scored.reduce((sum, r) => sum + (r.score ?? 0), 0) / scored.length : null;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col p-0 sm:max-w-lg">
        <SheetHeader className="border-b px-6 pb-4 pt-6">
          <SheetTitle className="flex items-center gap-2">
            <Play className="h-5 w-5" />
            Run Tests
          </SheetTitle>
          <SheetDescription>
            Select an agent and the scenarios to run against it, one after another.
          </SheetDescription>
          <p className="mt-2 text-xs text-muted-foreground" data-testid="text-harness-notice">
            {TEXT_HARNESS_NOTICE}
          </p>
        </SheetHeader>

        <div className="flex flex-1 flex-col overflow-hidden">
          {/* Agent Selection */}
          <div className="border-b px-6 py-4">
            <div className="mb-2 flex items-center gap-2">
              <label className="text-sm font-medium">Select Agent</label>
              <InfoTooltip content="Choose which agent to test. Each selected scenario is simulated in text against this agent's prompt and tools." />
            </div>
            <Select value={selectedAgentId} onValueChange={setSelectedAgentId} disabled={isRunning}>
              <SelectTrigger>
                <SelectValue placeholder="Choose an agent to test" />
              </SelectTrigger>
              <SelectContent>
                {agents.map((agent) => (
                  <SelectItem key={agent.id} value={agent.id}>
                    {agent.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Scenario Selection or Results */}
          {!isRunning && !isComplete ? (
            <ScenarioSelection
              scenariosByCategory={scenariosByCategory}
              selectedScenarios={selectedScenarios}
              onToggle={handleScenarioToggle}
              onSelectAll={handleSelectAll}
              onClear={handleClearSelection}
            />
          ) : (
            <TestProgress
              scenarioResults={scenarioResults}
              progressPercent={progressPercent}
              completedCount={completedCount}
              totalCount={totalCount}
              expandedResults={expandedResults}
              onToggleExpanded={toggleResultExpanded}
              isRunning={isRunning}
            />
          )}

          {/* Footer */}
          <div className="mt-auto border-t px-6 py-4">
            {/* Summary Stats */}
            {runError && (
              <div
                role="alert"
                className="mb-4 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>The run stopped: {runError}</span>
              </div>
            )}
            {(isRunning || isComplete) && (
              <div className="mb-4 rounded-lg bg-muted/50 p-3">
                <div className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-4">
                    <span className="flex items-center gap-1 text-green-600">
                      <CheckCircle className="h-4 w-4" />
                      {passedCount} passed
                    </span>
                    <span className="flex items-center gap-1 text-red-600">
                      <XCircle className="h-4 w-4" />
                      {failedCount} failed
                    </span>
                    {errorCount > 0 && (
                      <span className="flex items-center gap-1 text-muted-foreground">
                        <AlertCircle className="h-4 w-4" />
                        {errorCount} not run
                      </span>
                    )}
                    {runningCount > 0 && (
                      <span className="flex items-center gap-1 text-blue-600">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        {runningCount} running
                      </span>
                    )}
                  </div>
                  {avgScore !== null && (
                    <Badge variant="secondary">Avg: {avgScore.toFixed(1)}</Badge>
                  )}
                </div>
              </div>
            )}

            {/* Action Buttons */}
            {isComplete ? (
              <div className="flex gap-2">
                <Button variant="outline" className="flex-1" onClick={handleReset}>
                  <RotateCcw className="mr-2 h-4 w-4" />
                  Run Again
                </Button>
                <Button className="flex-1" onClick={() => onOpenChange(false)}>
                  Done
                </Button>
              </div>
            ) : (
              <Button
                className="w-full"
                onClick={handleStartTest}
                disabled={!selectedAgentId || selectedScenarios.length === 0 || isRunning}
              >
                {isRunning ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Running Tests...
                  </>
                ) : (
                  <>
                    <Play className="mr-2 h-4 w-4" />
                    Run {selectedScenarios.length} Test
                    {selectedScenarios.length !== 1 ? "s" : ""}
                  </>
                )}
              </Button>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

// =============================================================================
// Sub-components
// =============================================================================

interface ScenarioSelectionProps {
  scenariosByCategory: Record<string, TestScenario[]>;
  selectedScenarios: string[];
  onToggle: (id: string) => void;
  onSelectAll: () => void;
  onClear: () => void;
}

function ScenarioSelection({
  scenariosByCategory,
  selectedScenarios,
  onToggle,
  onSelectAll,
  onClear,
}: ScenarioSelectionProps) {
  const totalScenarios = Object.values(scenariosByCategory).flat().length;

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b px-6 py-3">
        <label className="text-sm font-medium">Select Scenarios</label>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={onSelectAll}>
            {selectedScenarios.length === totalScenarios ? "Deselect All" : "Select All"}
          </Button>
          {selectedScenarios.length > 0 && (
            <Button variant="ghost" size="sm" onClick={onClear}>
              Clear
            </Button>
          )}
        </div>
      </div>

      <ScrollArea className="flex-1">
        <div className="space-y-4 px-6 py-3">
          {Object.entries(scenariosByCategory).map(([category, categoryScenarios]) => (
            <div key={category}>
              <h4 className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                {category.replace("_", " ")}
              </h4>
              <div className="space-y-1">
                {categoryScenarios.map((scenario) => (
                  <label
                    key={scenario.id}
                    className={cn(
                      "flex cursor-pointer items-center gap-3 rounded-md p-2 transition-colors",
                      "hover:bg-muted/50",
                      selectedScenarios.includes(scenario.id) && "bg-muted"
                    )}
                  >
                    <Checkbox
                      checked={selectedScenarios.includes(scenario.id)}
                      onCheckedChange={() => onToggle(scenario.id)}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm">{scenario.name}</span>
                        {scenario.is_built_in && (
                          <Badge variant="secondary" className="shrink-0 text-[10px]">
                            Built-in
                          </Badge>
                        )}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <DifficultyBadge difficulty={scenario.difficulty} />
                    </div>
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
      </ScrollArea>

      <div className="border-t px-6 py-2 text-sm text-muted-foreground">
        {selectedScenarios.length} scenario{selectedScenarios.length !== 1 ? "s" : ""} selected
      </div>
    </div>
  );
}

interface TestProgressProps {
  scenarioResults: ScenarioResult[];
  progressPercent: number;
  completedCount: number;
  totalCount: number;
  expandedResults: Set<string>;
  onToggleExpanded: (id: string) => void;
  isRunning: boolean;
}

function TestProgress({
  scenarioResults,
  progressPercent,
  completedCount,
  totalCount,
  expandedResults,
  onToggleExpanded,
  isRunning: _isRunning,
}: TestProgressProps) {
  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Progress Bar */}
      <div className="border-b px-6 py-4">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-sm font-medium">Test Progress</span>
          <span className="text-sm text-muted-foreground">
            {completedCount}/{totalCount} Complete
          </span>
        </div>
        <Progress value={progressPercent} className="h-2" />
      </div>

      {/* Results List */}
      <ScrollArea className="flex-1">
        <div className="space-y-2 px-6 py-3">
          {scenarioResults.map((result) => (
            <ScenarioResultItem
              key={result.scenario_id}
              result={result}
              isExpanded={expandedResults.has(result.scenario_id)}
              onToggleExpanded={() => onToggleExpanded(result.scenario_id)}
            />
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}

interface ScenarioResultItemProps {
  result: ScenarioResult;
  isExpanded: boolean;
  onToggleExpanded: () => void;
}

function ScenarioResultItem({ result, isExpanded, onToggleExpanded }: ScenarioResultItemProps) {
  const getStatusIcon = () => {
    switch (result.status) {
      case "passed":
        return <CheckCircle className="h-4 w-4 text-green-600" />;
      case "failed":
        return <XCircle className="h-4 w-4 text-red-600" />;
      case "error":
        return <AlertCircle className="h-4 w-4 text-muted-foreground" />;
      case "running":
        return <Loader2 className="h-4 w-4 animate-spin text-blue-600" />;
      default:
        return <Clock className="h-4 w-4 text-muted-foreground" />;
    }
  };

  const getStatusText = () => {
    switch (result.status) {
      case "passed":
        return "PASS";
      case "failed":
        return "FAIL";
      case "error":
        return "NOT RUN";
      case "running":
        return "Running...";
      default:
        return "Pending";
    }
  };

  const hasDetails = result.failure_reason ?? result.duration_ms;

  return (
    <div
      className={cn(
        "rounded-lg border transition-colors",
        result.status === "passed" && "border-green-200 bg-green-50/50",
        result.status === "failed" && "border-red-200 bg-red-50/50",
        result.status === "running" && "border-blue-200 bg-blue-50/50",
        result.status === "pending" && "border-muted"
      )}
    >
      <button
        className="flex w-full items-center gap-3 p-3 text-left"
        onClick={hasDetails ? onToggleExpanded : undefined}
        disabled={!hasDetails}
      >
        {getStatusIcon()}
        <div className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{result.scenario_name}</span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {result.score !== undefined && (
            <Badge
              variant="secondary"
              className={cn(
                result.score >= 80 && "bg-green-100 text-green-800",
                result.score >= 60 && result.score < 80 && "bg-yellow-100 text-yellow-800",
                result.score < 60 && "bg-red-100 text-red-800"
              )}
            >
              {result.score}
            </Badge>
          )}
          <Badge
            variant={
              result.status === "passed"
                ? "default"
                : result.status === "failed"
                  ? "destructive"
                  : "secondary"
            }
            className="text-xs"
          >
            {getStatusText()}
          </Badge>
          {hasDetails &&
            (isExpanded ? (
              <ChevronUp className="h-4 w-4 text-muted-foreground" />
            ) : (
              <ChevronDown className="h-4 w-4 text-muted-foreground" />
            ))}
        </div>
      </button>

      {/* Expanded Details */}
      {isExpanded && hasDetails && (
        <div className="px-3 pb-3 pt-0">
          <Separator className="mb-3" />
          <div className="space-y-2 text-sm">
            {result.failure_reason && (
              <div className="flex items-start gap-2">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
                <div>
                  <span className="font-medium text-red-700">
                    {result.status === "error" ? "Why it did not run:" : "Failure Reason:"}
                  </span>
                  <p className="mt-1 text-muted-foreground">{result.failure_reason}</p>
                </div>
              </div>
            )}
            {result.duration_ms !== undefined && (
              <div className="flex items-center gap-2 text-muted-foreground">
                <Clock className="h-4 w-4" />
                <span>Duration: {(result.duration_ms / 1000).toFixed(2)}s</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function DifficultyBadge({ difficulty }: { difficulty: string }) {
  const variant =
    difficulty === "easy"
      ? "bg-green-100 text-green-800"
      : difficulty === "medium"
        ? "bg-yellow-100 text-yellow-800"
        : "bg-red-100 text-red-800";

  return (
    <Badge variant="secondary" className={cn("text-[10px] capitalize", variant)}>
      {difficulty}
    </Badge>
  );
}

export default TestRunner;
