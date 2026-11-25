"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { RehearsalMark } from "@/components/caller/rehearsal-mark";
import { EmptyState, ErrorState, LoadingState } from "@/components/caller/page-state";
import { errorMessage, formatInterval, formatRate, listCallerRuns } from "@/lib/api/caller-runs";

const PAGE_SIZE = 20;

export default function CallerRunsPage() {
  const [page, setPage] = useState(1);
  const { data, isLoading, error } = useQuery({
    queryKey: ["caller-runs", page],
    queryFn: () => listCallerRuns({ page, page_size: PAGE_SIZE }),
  });

  const runs = data?.runs ?? [];
  const total = data?.total ?? 0;
  const totalPages = data?.total_pages ?? 0;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Caller runs</h1>
        <p className="text-sm text-muted-foreground">
          {isLoading
            ? "Loading..."
            : total === 0
              ? "Batches of test calls placed into a voice agent and graded from what it stored."
              : `${total} ${total === 1 ? "run" : "runs"} uploaded`}
        </p>
      </div>

      <Card>
        <CardContent className="pt-6">
          {isLoading ? (
            <LoadingState label="Loading caller runs..." />
          ) : error ? (
            <ErrorState
              title="Failed to load caller runs"
              message={errorMessage(error, "The server did not answer.")}
            />
          ) : runs.length === 0 ? (
            <EmptyState title="No caller runs yet">
              <p>Run a batch with the caller, then send it here from the machine that ran it:</p>
              <pre className="mt-3 overflow-x-auto rounded-md bg-muted p-3 text-left font-mono text-xs text-foreground">
                synthiq-caller upload runs/ --api &lt;this server&gt; --workspace &lt;workspace
                id&gt;
              </pre>
            </EmptyState>
          ) : (
            <>
              <Table>
                <caption className="sr-only">Uploaded caller runs, newest first</caption>
                <TableHeader>
                  <TableRow>
                    <TableHead scope="col">Run</TableHead>
                    <TableHead scope="col">Uploaded</TableHead>
                    <TableHead scope="col">Workspace</TableHead>
                    <TableHead scope="col">Passed, of graded</TableHead>
                    <TableHead scope="col">Passed, of attempted</TableHead>
                    <TableHead scope="col" className="text-right">
                      Attempted
                    </TableHead>
                    <TableHead scope="col" className="text-right">
                      Left out
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runs.map((run) => (
                    <TableRow key={run.id}>
                      <TableCell>
                        <Link
                          href={`/dashboard/caller-runs/${run.id}`}
                          className="font-medium underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          {run.target_label || run.run_id}
                        </Link>
                        {run.target_label && (
                          <div className="font-mono text-xs text-muted-foreground">
                            {run.run_id}
                          </div>
                        )}
                        {run.rehearsal && <RehearsalMark className="mt-1" />}
                        {(run.outcome_counts.synthetic_calls ?? 0) > 0 && (
                          <span className="mr-2 mt-1 block text-xs text-muted-foreground">
                            synthetic voice in {run.outcome_counts.synthetic_calls} of{" "}
                            {run.attempted} calls
                          </span>
                        )}
                        {(run.outcome_counts.labels_unverified_calls ?? 0) > 0 && (
                          <span className="block text-xs text-muted-foreground">
                            labels unverified in {run.outcome_counts.labels_unverified_calls} of{" "}
                            {run.attempted} calls
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-sm">
                        {new Date(run.uploaded_at).toLocaleString()}
                      </TableCell>
                      <TableCell className="text-sm">{run.workspace_name ?? "-"}</TableCell>
                      <TableCell>
                        <div className="font-medium">{formatRate(run.of_graded)}</div>
                        <div className="text-xs text-muted-foreground">
                          {formatInterval(run.of_graded)}
                        </div>
                      </TableCell>
                      <TableCell>{formatRate(run.of_attempted, "no calls placed")}</TableCell>
                      <TableCell className="text-right tabular-nums">{run.attempted}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {run.excluded_count}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>

              {totalPages > 1 && (
                <nav
                  aria-label="Pages of caller runs"
                  className="mt-4 flex items-center justify-between border-t pt-4"
                >
                  <p className="text-sm text-muted-foreground">
                    Page {page} of {totalPages}
                  </p>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setPage((current) => Math.max(1, current - 1))}
                      disabled={page === 1}
                    >
                      <ChevronLeft className="mr-1 h-4 w-4" aria-hidden />
                      Previous
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
                      disabled={page === totalPages}
                    >
                      Next
                      <ChevronRight className="ml-1 h-4 w-4" aria-hidden />
                    </Button>
                  </div>
                </nav>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
