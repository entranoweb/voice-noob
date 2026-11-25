import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** Says a run was placed against a scripted stand-in, so it cannot be mistaken for a test of an agent. */
export function RehearsalMark({ className }: { className?: string }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "whitespace-nowrap border-amber-600/50 bg-amber-500/10 font-semibold text-amber-800 dark:text-amber-400",
        className
      )}
    >
      REHEARSAL (scripted agent)
    </Badge>
  );
}
