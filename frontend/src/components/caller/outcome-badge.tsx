import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * A call's outcome as the caller rig wrote it.
 *
 * Only "passed" and "failed" are verdicts on the agent. Anything else (ungraded,
 * not run, or an outcome this page has never seen) is shown neutrally: it proves
 * nothing either way, and colouring it red would blame the agent for it.
 */
export function OutcomeBadge({ outcome, className }: { outcome: string; className?: string }) {
  const tone =
    outcome === "passed"
      ? "border-emerald-600/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
      : outcome === "failed"
        ? "border-red-600/40 bg-red-500/10 text-red-700 dark:text-red-400"
        : "border-border bg-muted text-muted-foreground";
  return (
    <Badge variant="outline" className={cn("whitespace-nowrap font-medium", tone, className)}>
      {outcome}
    </Badge>
  );
}
