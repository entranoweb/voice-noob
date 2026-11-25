"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Loader2, Trash2 } from "lucide-react";
import { apiTokensApi } from "@/lib/api/api-tokens";
import { CopyButton } from "@/components/settings/copy-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const QUERY_KEY = ["api-tokens"];

function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "never";
}

/** Create, list and revoke personal API tokens. A new token's secret is shown once. */
export function ApiTokensPanel() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [expiresInDays, setExpiresInDays] = useState("");
  const [shownOnce, setShownOnce] = useState<{ name: string; token: string } | null>(null);

  const { data: tokens = [], isLoading } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: apiTokensApi.list,
  });

  const create = useMutation({
    mutationFn: () =>
      apiTokensApi.create(name.trim(), expiresInDays ? Number(expiresInDays) : undefined),
    onSuccess: (created) => {
      setShownOnce({ name: created.name, token: created.token });
      setName("");
      setExpiresInDays("");
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
    onError: () => toast.error("Could not create the token"),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => apiTokensApi.revoke(id),
    onSuccess: () => {
      toast.success("Token revoked");
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
    onError: () => toast.error("Could not revoke the token"),
  });

  const active = tokens.filter((token) => !token.revoked_at);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-medium">API tokens</h2>
        <p className="text-sm text-muted-foreground">
          A token lets <code>synthiq-caller upload</code> send runs and their audio to your
          workspaces, and nothing else. Set it as <code>SYNTHIQ_API_TOKEN</code>. Resetting your
          password revokes every token.
        </p>
      </div>

      {shownOnce && (
        <div
          role="status"
          className="space-y-2 rounded-md border border-primary/40 bg-primary/5 p-4 text-sm"
        >
          <p className="font-medium">
            Token &ldquo;{shownOnce.name}&rdquo; created. Copy it now; it is not shown again.
          </p>
          <div className="flex items-center gap-2">
            <code
              data-testid="new-api-token"
              className="select-all break-all rounded bg-muted px-2 py-1 font-mono text-xs"
            >
              {shownOnce.token}
            </code>
            <CopyButton value={shownOnce.token} label="token" />
          </div>
          <Button type="button" variant="ghost" size="sm" onClick={() => setShownOnce(null)}>
            I have copied it
          </Button>
        </div>
      )}

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) create.mutate();
        }}
      >
        <div className="space-y-1">
          <Label htmlFor="token-name">Token name</Label>
          <Input
            id="token-name"
            value={name}
            maxLength={100}
            placeholder="CI upload"
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="token-expiry">Expires after (days, optional)</Label>
          <Input
            id="token-expiry"
            type="number"
            min={1}
            max={3650}
            value={expiresInDays}
            onChange={(e) => setExpiresInDays(e.target.value)}
          />
        </div>
        <Button type="submit" disabled={!name.trim() || create.isPending}>
          {create.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          Create token
        </Button>
      </form>

      <div className="space-y-2">
        {isLoading && <p className="text-sm text-muted-foreground">Loading tokens...</p>}
        {!isLoading && tokens.length === 0 && (
          <p className="text-sm text-muted-foreground">No tokens yet.</p>
        )}
        {tokens.map((token) => (
          <Card key={token.id}>
            <CardContent className="flex flex-wrap items-center justify-between gap-3 p-4">
              <div className="space-y-1 text-sm">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{token.name}</span>
                  <code className="font-mono text-xs text-muted-foreground">
                    {token.token_prefix}...
                  </code>
                  {token.revoked_at && <Badge variant="secondary">Revoked</Badge>}
                </div>
                <p className="text-xs text-muted-foreground">
                  Created {formatDate(token.created_at)} · Last used{" "}
                  {formatDate(token.last_used_at)}
                  {token.expires_at ? ` · Expires ${formatDate(token.expires_at)}` : ""}
                </p>
              </div>
              {!token.revoked_at && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  aria-label={`Revoke ${token.name}`}
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(token.id)}
                >
                  <Trash2 className="mr-1 h-3.5 w-3.5" />
                  Revoke
                </Button>
              )}
            </CardContent>
          </Card>
        ))}
        {active.length > 0 && (
          <p className="text-xs text-muted-foreground">{active.length} active</p>
        )}
      </div>
    </div>
  );
}
