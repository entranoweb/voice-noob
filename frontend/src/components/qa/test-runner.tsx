"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { Play, Loader2, CheckCircle, XCircle } from "lucide-react";
import { fetchAgents } from "@/lib/api/agents";
import { listScenarios, startTestRun, getTestRun } from "@/lib/api/qa";
import type { TestScenario, TestRun } from "@/lib/api/qa";

interface TestRunnerProps {
  onTestComplete?: (run: TestRun) => void;
}

export function TestRunner({ onTestComplete }: TestRunnerProps) {
  const [selectedAgentId, setSelectedAgentId] = useState<string>("");
  const [selectedScenarios, setSelectedScenarios] = useState<string[]>([]);
  const [currentRun, setCurrentRun] = useState<TestRun | null>(null);

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

  // Start test run mutation
  const startRunMutation = useMutation({
    mutationFn: startTestRun,
    onSuccess: (run) => {
      setCurrentRun(run);
      // Poll for completion
      void pollForCompletion(run.id);
    },
  });

  const pollForCompletion = async (runId: string) => {
    const checkStatus = async () => {
      try {
        const run = await getTestRun(runId);
        setCurrentRun(run);

        if (run.status === "completed" || run.status === "failed") {
          onTestComplete?.(run);
          return;
        }

        // Continue polling
        setTimeout(() => void checkStatus(), 2000);
      } catch {
        // Polling error, stop
      }
    };

    await checkStatus();
  };

  const handleScenarioToggle = (scenarioId: string) => {
    setSelectedScenarios((prev) =>
      prev.includes(scenarioId)
        ? prev.filter((id) => id !== scenarioId)
        : [...prev, scenarioId]
    );
  };

  const handleSelectAll = () => {
    if (selectedScenarios.length === scenarios.length) {
      setSelectedScenarios([]);
    } else {
      setSelectedScenarios(scenarios.map((s) => s.id));
    }
  };

  const handleStartTest = () => {
    if (!selectedAgentId || selectedScenarios.length === 0) return;

    startRunMutation.mutate({
      agent_id: selectedAgentId,
      scenario_ids: selectedScenarios,
    });
  };

  const isRunning = currentRun?.status === "running" || currentRun?.status === "pending";

  // Group scenarios by category
  const scenariosByCategory = scenarios.reduce(
    (acc, scenario) => {
      const category = scenario.category ?? "other";
      acc[category] ??= [];
      acc[category].push(scenario);
      return acc;
    },
    {} as Record<string, TestScenario[]>
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-medium">Test Runner</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Agent Selection */}
        <div>
          <label className="text-sm font-medium mb-2 block">Select Agent</label>
          <Select value={selectedAgentId} onValueChange={setSelectedAgentId}>
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

        {/* Scenario Selection */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-sm font-medium">Test Scenarios</label>
            <Button variant="ghost" size="sm" onClick={handleSelectAll}>
              {selectedScenarios.length === scenarios.length ? "Deselect All" : "Select All"}
            </Button>
          </div>

          <div className="max-h-64 overflow-y-auto space-y-4 border rounded-md p-3">
            {Object.entries(scenariosByCategory).map(([category, categoryScenarios]) => (
              <div key={category}>
                <h4 className="text-xs font-medium text-muted-foreground uppercase mb-2">
                  {category.replace("_", " ")}
                </h4>
                <div className="space-y-2">
                  {categoryScenarios.map((scenario) => (
                    <div key={scenario.id} className="flex items-center gap-2">
                      <Checkbox
                        id={scenario.id}
                        checked={selectedScenarios.includes(scenario.id)}
                        onCheckedChange={() => handleScenarioToggle(scenario.id)}
                        disabled={isRunning}
                      />
                      <label
                        htmlFor={scenario.id}
                        className="text-sm cursor-pointer flex-1 flex items-center gap-2"
                      >
                        {scenario.name}
                        {scenario.is_built_in && (
                          <Badge variant="secondary" className="text-[10px]">
                            Built-in
                          </Badge>
                        )}
                      </label>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Run Button */}
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
              Run {selectedScenarios.length} Test{selectedScenarios.length !== 1 ? "s" : ""}
            </>
          )}
        </Button>

        {/* Results */}
        {currentRun?.status === "completed" && (
          <div className="border rounded-md p-3 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">Test Results</span>
              <Badge variant={currentRun.pass_rate && currentRun.pass_rate >= 0.7 ? "default" : "destructive"}>
                {currentRun.pass_rate ? `${(currentRun.pass_rate * 100).toFixed(0)}% Pass Rate` : "N/A"}
              </Badge>
            </div>
            <div className="flex items-center gap-4 text-sm">
              <div className="flex items-center gap-1 text-green-600">
                <CheckCircle className="h-4 w-4" />
                {currentRun.passed_scenarios} passed
              </div>
              <div className="flex items-center gap-1 text-red-600">
                <XCircle className="h-4 w-4" />
                {currentRun.failed_scenarios} failed
              </div>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
