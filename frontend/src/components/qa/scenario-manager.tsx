"use client";

import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Plus,
  Search,
  Loader2,
  Database,
  Filter,
  FolderOpen,
  AlertTriangle,
} from "lucide-react";
import { listScenarios, seedScenarios, type TestScenario } from "@/lib/api/qa";
import { ScenarioCard } from "./scenario-card";
import { InfoTooltip } from "@/components/ui/info-tooltip";

// =============================================================================
// Types
// =============================================================================

interface ScenarioManagerProps {
  workspaceId?: string;
  onCreateScenario?: () => void;
  onEditScenario?: (scenario: TestScenario) => void;
  onViewScenario?: (scenario: TestScenario) => void;
  selectedScenarioIds?: string[];
  onSelectionChange?: (ids: string[]) => void;
}

type FilterType = "all" | "built-in" | "custom";
type CategoryFilter = "all" | string;
type DifficultyFilter = "all" | "easy" | "medium" | "hard";

// =============================================================================
// Constants
// =============================================================================

const CATEGORIES = [
  { value: "all", label: "All Categories" },
  { value: "greeting", label: "Greeting" },
  { value: "booking", label: "Booking" },
  { value: "inquiry", label: "Inquiry" },
  { value: "objection", label: "Objection" },
  { value: "support", label: "Support" },
  { value: "sales", label: "Sales" },
  { value: "custom", label: "Custom" },
];

const DIFFICULTIES = [
  { value: "all", label: "All Difficulties" },
  { value: "easy", label: "Easy" },
  { value: "medium", label: "Medium" },
  { value: "hard", label: "Hard" },
];

// =============================================================================
// Component
// =============================================================================

export function ScenarioManager({
  workspaceId: _workspaceId,
  onCreateScenario,
  onEditScenario,
  onViewScenario,
  selectedScenarioIds = [],
  onSelectionChange,
}: ScenarioManagerProps) {
  const queryClient = useQueryClient();

  // Filter state
  const [searchQuery, setSearchQuery] = useState("");
  const [filterType, setFilterType] = useState<FilterType>("all");
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter>("all");
  const [difficultyFilter, setDifficultyFilter] = useState<DifficultyFilter>("all");

  // Fetch scenarios
  const {
    data: scenarios = [],
    isLoading,
    error,
  } = useQuery({
    queryKey: ["qa-scenarios", categoryFilter !== "all" ? categoryFilter : undefined],
    queryFn: () =>
      listScenarios({
        category: categoryFilter !== "all" ? categoryFilter : undefined,
      }),
  });

  // Seed scenarios mutation
  const seedMutation = useMutation({
    mutationFn: seedScenarios,
    onSuccess: (data) => {
      if (data.scenarios_created > 0) {
        toast.success(`Seeded ${data.scenarios_created} built-in scenarios`);
      } else {
        toast.info("Built-in scenarios already exist");
      }
      void queryClient.invalidateQueries({ queryKey: ["qa-scenarios"] });
    },
    onError: (error: Error) => {
      toast.error(error.message ?? "Failed to seed scenarios");
    },
  });

  // Filter and search scenarios
  const filteredScenarios = useMemo(() => {
    let result = scenarios;

    // Filter by type (built-in/custom)
    if (filterType === "built-in") {
      result = result.filter((s) => s.is_built_in);
    } else if (filterType === "custom") {
      result = result.filter((s) => !s.is_built_in);
    }

    // Filter by difficulty
    if (difficultyFilter !== "all") {
      result = result.filter((s) => s.difficulty === difficultyFilter);
    }

    // Search by name or description
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      result = result.filter(
        (s) =>
          s.name.toLowerCase().includes(query) ||
          (s.description?.toLowerCase().includes(query) ?? false) ||
          s.category.toLowerCase().includes(query)
      );
    }

    return result;
  }, [scenarios, filterType, difficultyFilter, searchQuery]);

  // Group scenarios by category
  const groupedScenarios = useMemo(() => {
    const groups: Record<string, TestScenario[]> = {};
    for (const scenario of filteredScenarios) {
      const category = scenario.category ?? "uncategorized";
      groups[category] ??= [];
      groups[category].push(scenario);
    }
    // Sort categories alphabetically
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
  }, [filteredScenarios]);

  // Handle scenario selection
  const handleToggleSelection = (scenarioId: string) => {
    if (!onSelectionChange) return;
    const newSelection = selectedScenarioIds.includes(scenarioId)
      ? selectedScenarioIds.filter((id) => id !== scenarioId)
      : [...selectedScenarioIds, scenarioId];
    onSelectionChange(newSelection);
  };

  // Handle select all in category
  const handleSelectCategory = (categoryScenarios: TestScenario[]) => {
    if (!onSelectionChange) return;
    const categoryIds = categoryScenarios.map((s) => s.id);
    const allSelected = categoryIds.every((id) => selectedScenarioIds.includes(id));
    if (allSelected) {
      // Deselect all in category
      onSelectionChange(selectedScenarioIds.filter((id) => !categoryIds.includes(id)));
    } else {
      // Select all in category
      const newSelection = [...new Set([...selectedScenarioIds, ...categoryIds])];
      onSelectionChange(newSelection);
    }
  };

  if (error) {
    const errorMessage = error instanceof Error ? error.message : "Unknown error";
    return (
      <Card className="border-destructive/50">
        <CardContent className="flex flex-col items-center justify-center py-8">
          <AlertTriangle className="mb-2 h-8 w-8 text-destructive" />
          <p className="text-sm text-muted-foreground">Failed to load scenarios</p>
          <p className="text-xs text-muted-foreground">{errorMessage}</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold">Test Scenarios</h2>
            <InfoTooltip 
              content="Test scenarios simulate different caller situations to verify your voice agent handles them correctly. Each scenario defines a caller persona, conversation flow, and success criteria."
              side="right"
            />
          </div>
          <p className="text-sm text-muted-foreground">
            Create and manage test scenarios for your voice agents
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => seedMutation.mutate()}
            disabled={seedMutation.isPending}
            title="Load pre-built test scenarios covering common voice agent use cases"
          >
            {seedMutation.isPending ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Database className="mr-2 h-4 w-4" />
            )}
            Seed Built-in
          </Button>
          <InfoTooltip 
            content="Seed Built-in: Loads pre-configured test scenarios covering common use cases like greetings, bookings, and objection handling. Great for getting started quickly!"
          />
          {onCreateScenario && (
            <Button size="sm" onClick={onCreateScenario}>
              <Plus className="mr-2 h-4 w-4" />
              Create Scenario
            </Button>
          )}
        </div>
      </div>

      {/* Filters */}
      <Card>
        <CardContent className="p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            {/* Search */}
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                placeholder="Search scenarios..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-9"
              />
            </div>

            {/* Filter by type */}
            <Select value={filterType} onValueChange={(v) => setFilterType(v as FilterType)}>
              <SelectTrigger className="w-[140px]">
                <Filter className="mr-2 h-4 w-4" />
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Types</SelectItem>
                <SelectItem value="built-in">Built-in</SelectItem>
                <SelectItem value="custom">Custom</SelectItem>
              </SelectContent>
            </Select>

            {/* Filter by category */}
            <Select
              value={categoryFilter}
              onValueChange={setCategoryFilter}
            >
              <SelectTrigger className="w-[160px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CATEGORIES.map((cat) => (
                  <SelectItem key={cat.value} value={cat.value}>
                    {cat.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {/* Filter by difficulty */}
            <Select
              value={difficultyFilter}
              onValueChange={(v) => setDifficultyFilter(v as DifficultyFilter)}
            >
              <SelectTrigger className="w-[150px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DIFFICULTIES.map((diff) => (
                  <SelectItem key={diff.value} value={diff.value}>
                    {diff.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      {/* Scenario List */}
      {isLoading ? (
        <ScenarioListSkeleton />
      ) : filteredScenarios.length === 0 ? (
        <EmptyState
          hasScenarios={scenarios.length > 0}
          onSeed={() => seedMutation.mutate()}
          onCreateScenario={onCreateScenario}
        />
      ) : (
        <div className="space-y-4">
          {groupedScenarios.map(([category, categoryScenarios]) => (
            <CategoryGroup
              key={category}
              category={category}
              scenarios={categoryScenarios}
              selectedIds={selectedScenarioIds}
              onToggleSelection={onSelectionChange ? handleToggleSelection : undefined}
              onSelectCategory={onSelectionChange ? () => handleSelectCategory(categoryScenarios) : undefined}
              onView={onViewScenario}
              onEdit={onEditScenario}
            />
          ))}
        </div>
      )}

      {/* Selection summary */}
      {onSelectionChange && selectedScenarioIds.length > 0 && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 rounded-lg border bg-background px-4 py-2 shadow-lg">
          <span className="text-sm">
            <strong>{selectedScenarioIds.length}</strong> scenario
            {selectedScenarioIds.length !== 1 ? "s" : ""} selected
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="ml-2"
            onClick={() => onSelectionChange([])}
          >
            Clear
          </Button>
        </div>
      )}
    </div>
  );
}


// =============================================================================
// Sub-components
// =============================================================================

interface CategoryGroupProps {
  category: string;
  scenarios: TestScenario[];
  selectedIds: string[];
  onToggleSelection?: (id: string) => void;
  onSelectCategory?: () => void;
  onView?: (scenario: TestScenario) => void;
  onEdit?: (scenario: TestScenario) => void;
}

function CategoryGroup({
  category,
  scenarios,
  selectedIds,
  onToggleSelection,
  onSelectCategory,
  onView,
  onEdit,
}: CategoryGroupProps) {
  const allSelected = scenarios.every((s) => selectedIds.includes(s.id));
  const someSelected = scenarios.some((s) => selectedIds.includes(s.id));

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-sm font-medium capitalize">
            <FolderOpen className="h-4 w-4 text-muted-foreground" />
            {category}
            <Badge variant="secondary" className="ml-1 text-xs">
              {scenarios.length}
            </Badge>
          </CardTitle>
          {onSelectCategory && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs"
              onClick={onSelectCategory}
            >
              {allSelected ? "Deselect All" : someSelected ? "Select All" : "Select All"}
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {scenarios.map((scenario) => (
          <ScenarioCard
            key={scenario.id}
            scenario={scenario}
            isSelected={selectedIds.includes(scenario.id)}
            onToggleSelection={onToggleSelection}
            onView={onView}
            onEdit={onEdit}
          />
        ))}
      </CardContent>
    </Card>
  );
}

function ScenarioListSkeleton() {
  return (
    <div className="space-y-4">
      {[1, 2, 3].map((i) => (
        <Card key={i}>
          <CardHeader className="pb-2">
            <Skeleton className="h-5 w-32" />
          </CardHeader>
          <CardContent className="space-y-2">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

interface EmptyStateProps {
  hasScenarios: boolean;
  onSeed: () => void;
  onCreateScenario?: () => void;
}

function EmptyState({ hasScenarios, onSeed, onCreateScenario }: EmptyStateProps) {
  if (hasScenarios) {
    // Has scenarios but filters returned nothing
    return (
      <Card>
        <CardContent className="flex flex-col items-center justify-center py-12">
          <Search className="mb-3 h-10 w-10 text-muted-foreground/30" />
          <p className="text-sm font-medium">No matching scenarios</p>
          <p className="text-xs text-muted-foreground">
            Try adjusting your filters or search query
          </p>
        </CardContent>
      </Card>
    );
  }

  // No scenarios at all
  return (
    <Card>
      <CardContent className="flex flex-col items-center justify-center py-12">
        <Database className="mb-3 h-10 w-10 text-muted-foreground/30" />
        <p className="text-sm font-medium">No test scenarios yet</p>
        <p className="mb-4 text-xs text-muted-foreground">
          Seed built-in scenarios or create your own
        </p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={onSeed}>
            <Database className="mr-2 h-4 w-4" />
            Seed Built-in
          </Button>
          {onCreateScenario && (
            <Button size="sm" onClick={onCreateScenario}>
              <Plus className="mr-2 h-4 w-4" />
              Create Scenario
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export default ScenarioManager;
