"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Eye,
  Pencil,
  Trash2,
  Copy,
  MoreHorizontal,
  Lock,
  Loader2,
} from "lucide-react";
import { useState } from "react";
import { deleteScenario, duplicateScenario, type TestScenario } from "@/lib/api/qa";

// =============================================================================
// Types
// =============================================================================

interface ScenarioCardProps {
  scenario: TestScenario;
  isSelected?: boolean;
  onToggleSelection?: (id: string) => void;
  onView?: (scenario: TestScenario) => void;
  onEdit?: (scenario: TestScenario) => void;
}

// =============================================================================
// Helpers
// =============================================================================

function getDifficultyColor(difficulty: string): string {
  switch (difficulty.toLowerCase()) {
    case "easy":
      return "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400";
    case "medium":
      return "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400";
    case "hard":
      return "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400";
    default:
      return "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400";
  }
}

// =============================================================================
// Component
// =============================================================================

export function ScenarioCard({
  scenario,
  isSelected = false,
  onToggleSelection,
  onView,
  onEdit,
}: ScenarioCardProps) {
  const queryClient = useQueryClient();
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: () => deleteScenario(scenario.id),
    onSuccess: () => {
      toast.success("Scenario deleted");
      void queryClient.invalidateQueries({ queryKey: ["qa-scenarios"] });
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to delete scenario");
    },
  });

  // Duplicate mutation
  const duplicateMutation = useMutation({
    mutationFn: () => duplicateScenario(scenario.id),
    onSuccess: (data) => {
      toast.success(`Created "${data.new_scenario.name}"`);
      void queryClient.invalidateQueries({ queryKey: ["qa-scenarios"] });
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to duplicate scenario");
    },
  });

  const handleDelete = () => {
    setShowDeleteDialog(false);
    deleteMutation.mutate();
  };

  const isLoading = deleteMutation.isPending || duplicateMutation.isPending;

  return (
    <>
      <div
        className={`flex items-center justify-between rounded-lg border p-3 transition-colors ${
          isSelected ? "border-primary bg-primary/5" : "hover:bg-muted/50"
        }`}
      >
        <div className="flex items-center gap-3">
          {/* Selection checkbox */}
          {onToggleSelection && (
            <Checkbox
              checked={isSelected}
              onCheckedChange={() => onToggleSelection(scenario.id)}
              aria-label={`Select ${scenario.name}`}
            />
          )}

          {/* Scenario info */}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="font-medium truncate">{scenario.name}</span>
              {scenario.is_built_in && (
                <Badge variant="secondary" className="text-xs">
                  <Lock className="mr-1 h-3 w-3" />
                  Built-in
                </Badge>
              )}
            </div>
            {scenario.description && (
              <p className="text-xs text-muted-foreground truncate max-w-md">
                {scenario.description}
              </p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* Difficulty badge */}
          <Badge className={`text-xs ${getDifficultyColor(scenario.difficulty)}`}>
            {scenario.difficulty}
          </Badge>

          {/* Actions */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon" className="h-8 w-8" disabled={isLoading}>
                {isLoading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <MoreHorizontal className="h-4 w-4" />
                )}
                <span className="sr-only">Actions</span>
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {onView && (
                <DropdownMenuItem onClick={() => onView(scenario)}>
                  <Eye className="mr-2 h-4 w-4" />
                  View Details
                </DropdownMenuItem>
              )}
              {onEdit && !scenario.is_built_in && (
                <DropdownMenuItem onClick={() => onEdit(scenario)}>
                  <Pencil className="mr-2 h-4 w-4" />
                  Edit
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                onClick={() => duplicateMutation.mutate()}
                disabled={duplicateMutation.isPending}
              >
                <Copy className="mr-2 h-4 w-4" />
                Duplicate
              </DropdownMenuItem>
              {!scenario.is_built_in && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    className="text-destructive focus:text-destructive"
                    onClick={() => setShowDeleteDialog(true)}
                    disabled={deleteMutation.isPending}
                  >
                    <Trash2 className="mr-2 h-4 w-4" />
                    Delete
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      {/* Delete confirmation dialog */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Scenario</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete &quot;{scenario.name}&quot;? This action cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

export default ScenarioCard;
