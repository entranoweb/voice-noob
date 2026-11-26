"use client";

import { CopyButton } from "@/components/settings/copy-button";

interface WorkspaceIdProps {
  workspaceId: string;
  workspaceName: string;
}

/** The id `synthiq-caller upload --workspace` needs, with a button that copies it. */
export function WorkspaceId({ workspaceId, workspaceName }: WorkspaceIdProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-muted-foreground">Workspace ID for {workspaceName}</span>
      <code
        data-testid="workspace-id"
        className="select-all rounded bg-muted px-2 py-1 font-mono text-xs"
      >
        {workspaceId}
      </code>
      <CopyButton value={workspaceId} label="workspace ID" />
    </div>
  );
}
