import type { ReactNode } from "react";
import { AlertCircle, Loader2 } from "lucide-react";

export function LoadingState({ label }: { label: string }) {
  return (
    <div role="status" className="flex flex-col items-center justify-center py-16">
      <Loader2 className="mb-4 h-10 w-10 animate-spin text-muted-foreground/50" aria-hidden />
      <p className="text-muted-foreground">{label}</p>
    </div>
  );
}

export function ErrorState({ title, message }: { title: string; message: string }) {
  return (
    <div role="alert" className="flex flex-col items-center justify-center py-16">
      <AlertCircle className="mb-4 h-10 w-10 text-destructive" aria-hidden />
      <h2 className="mb-2 text-lg font-semibold">{title}</h2>
      <p className="max-w-md text-center text-sm text-muted-foreground">{message}</p>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center py-16">
      <h2 className="mb-2 text-lg font-semibold">{title}</h2>
      <div className="max-w-xl text-center text-sm text-muted-foreground">{children}</div>
    </div>
  );
}
