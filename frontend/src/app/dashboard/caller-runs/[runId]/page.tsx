"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { OutcomeBadge } from "@/components/caller/outcome-badge";
import { ErrorState, LoadingState } from "@/components/caller/page-state";
import { RehearsalMark } from "@/components/caller/rehearsal-mark";
import {
  errorMessage,
  formatInterval,
  formatRate,
  getCallerRun,
  type CallerRunDetail,
} from "@/lib/api/caller-runs";

const linkClass =
  "font-medium underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function Headline({ run }: { run: CallerRunDetail }) {
  const interval = formatInterval(run.of_graded);
  return (
    <Card>
      <CardContent className="grid gap-6 pt-6 sm:grid-cols-2 lg:grid-cols-4">
        <div className="sm:col-span-2">
          <p className="text-sm text-muted-foreground">Passed, of graded calls</p>
          <p className="text-3xl font-semibold tabular-nums">{formatRate(run.of_graded)}</p>
          <p className="text-sm text-muted-foreground">
            {interval || "No call was graded, so there is no pass rate."}
          </p>
        </div>
        <div>
          <p className="text-sm text-muted-foreground">Passed, of every call placed</p>
          <p className="text-xl font-semibold tabular-nums">
            {formatRate(run.of_attempted, "no calls placed")}
          </p>
          <p className="text-sm text-muted-foreground">
            Counts calls that could not be graded as not passed.
          </p>
        </div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Placed</dt>
          <dd className="text-right tabular-nums">{run.attempted}</dd>
          <dt className="text-muted-foreground">Graded</dt>
          <dd className="text-right tabular-nums">{run.graded}</dd>
          <dt className="text-muted-foreground">Failed</dt>
          <dd className="text-right tabular-nums">{run.failed}</dd>
          <dt className="text-muted-foreground">Void (rig could not answer)</dt>
          <dd className="text-right tabular-nums">{run.outcome_counts.void ?? 0}</dd>
          <dt className="text-muted-foreground">Ungradeable</dt>
          <dd className="text-right tabular-nums">{run.outcome_counts.ungradeable ?? 0}</dd>
          <dt className="text-muted-foreground">Ungraded</dt>
          <dd className="text-right tabular-nums">{run.outcome_counts.ungraded ?? 0}</dd>
          <dt className="text-muted-foreground">Other, not graded</dt>
          <dd className="text-right tabular-nums">
            {run.not_graded -
              (run.outcome_counts.void ?? 0) -
              (run.outcome_counts.ungradeable ?? 0) -
              (run.outcome_counts.ungraded ?? 0)}
          </dd>
          <dt className="text-muted-foreground">Unavailable (tool result withheld)</dt>
          <dd className="text-right tabular-nums">{run.outcome_counts.unavailable_calls ?? 0}</dd>
          <dt className="text-muted-foreground">Not run</dt>
          <dd className="text-right tabular-nums">{run.not_run}</dd>
        </dl>
      </CardContent>
    </Card>
  );
}

/** What the test caller's voice and labels were, when that weakens what a pass proves. */
function Provenance({ run }: { run: CallerRunDetail }) {
  const synthetic = run.outcome_counts.synthetic_calls ?? 0;
  const unverified = run.outcome_counts.labels_unverified_calls ?? 0;
  if (synthetic === 0 && unverified === 0) return null;
  return (
    <Card>
      <CardContent className="space-y-1 pt-6 text-sm" role="note" aria-label="Clip provenance">
        {synthetic > 0 && (
          <p>
            <span className="font-medium">
              {synthetic} of {run.attempted} calls used a synthetic voice
            </span>{" "}
            for at least one answer, not a recorded caller.
          </p>
        )}
        {unverified > 0 && (
          <p>
            <span className="font-medium">
              {unverified} of {run.attempted} calls played a clip with an unverified label:
            </span>{" "}
            no person has confirmed what the clip says, so &quot;said&quot; is itself a guess.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function FieldsTable({ run }: { run: CallerRunDetail }) {
  if (run.fields.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Said, heard, stored across the run</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <caption className="sr-only">
            For each detail callers gave: how often it was stored correctly, only sent, misheard or
            stored wrongly
          </caption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Field</TableHead>
              <TableHead scope="col" className="text-right">
                Answered
              </TableHead>
              <TableHead scope="col" className="text-right">
                Stored correctly
              </TableHead>
              <TableHead scope="col" className="text-right">
                Sent, not confirmed stored
              </TableHead>
              <TableHead scope="col" className="text-right">
                Misheard
              </TableHead>
              <TableHead scope="col" className="text-right">
                Stored wrongly
              </TableHead>
              <TableHead scope="col" className="text-right">
                Not stored
              </TableHead>
              <TableHead scope="col" className="text-right">
                Labels unverified
              </TableHead>
              <TableHead scope="col" className="text-right">
                Synthetic voice
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {run.fields.map((row) => (
              <TableRow key={row.field}>
                <TableCell className="font-medium">{row.field}</TableCell>
                <TableCell className="text-right tabular-nums">{row.answered}</TableCell>
                <TableCell className="text-right tabular-nums">{row.stored_correctly}</TableCell>
                <TableCell className="text-right tabular-nums">{row.sent_correctly ?? 0}</TableCell>
                <TableCell className="text-right tabular-nums">{row.misheard}</TableCell>
                <TableCell className="text-right tabular-nums">{row.mis_stored}</TableCell>
                <TableCell className="text-right tabular-nums">{row.not_stored}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {row.labels_unverified ?? 0}
                </TableCell>
                <TableCell className="text-right tabular-nums">{row.synthetic ?? 0}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <p className="mt-3 text-xs text-muted-foreground">
          &quot;Stored correctly&quot; counts only values read back from the system afterwards.
          &quot;Sent, not confirmed stored&quot; means the agent&apos;s tool was sent the right
          value and nothing checked that the system kept it.
        </p>
      </CardContent>
    </Card>
  );
}

export default function CallerRunPage() {
  const params = useParams<{ runId: string }>();
  const runId = params.runId;
  const {
    data: run,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["caller-run", runId],
    queryFn: () => getCallerRun(runId),
    enabled: Boolean(runId),
  });

  return (
    <div className="space-y-4">
      <Link
        href="/dashboard/caller-runs"
        className={`inline-flex items-center text-sm ${linkClass}`}
      >
        <ChevronLeft className="mr-1 h-4 w-4" aria-hidden />
        All caller runs
      </Link>

      {isLoading ? (
        <LoadingState label="Loading run..." />
      ) : error || !run ? (
        <ErrorState
          title="Failed to load this run"
          message={errorMessage(error, "The server did not answer.")}
        />
      ) : (
        <>
          <div>
            <h1 className="text-xl font-semibold">{run.target_label || run.run_id}</h1>
            <p className="text-sm text-muted-foreground">
              Run <span className="font-mono">{run.run_id}</span>
              {run.workspace_name ? ` in ${run.workspace_name}` : ""}, uploaded{" "}
              {new Date(run.uploaded_at).toLocaleString()}
              {run.rig_version ? `, caller ${run.rig_version}` : ""}
            </p>
          </div>

          {run.rehearsal && (
            <Card role="note" aria-label="Rehearsal">
              <CardContent className="space-y-1 pt-6 text-sm">
                <RehearsalMark />
                <p>
                  These calls were placed against a scripted stand-in, not an agent
                  {(run.outcome_counts.rehearsal_calls ?? 0) > 0
                    ? ` (${run.outcome_counts.rehearsal_calls} of ${run.attempted} calls)`
                    : ""}
                  . They show that the rig runs; they show nothing about any agent.
                </p>
              </CardContent>
            </Card>
          )}

          <Headline run={run} />

          <Provenance run={run} />

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Per scenario</CardTitle>
            </CardHeader>
            <CardContent>
              {run.scenarios.length === 0 ? (
                <p className="text-sm text-muted-foreground">No call was placed in this run.</p>
              ) : (
                <Table>
                  <caption className="sr-only">Pass rate for each scenario</caption>
                  <TableHeader>
                    <TableRow>
                      <TableHead scope="col">Scenario</TableHead>
                      <TableHead scope="col">Passed, of graded</TableHead>
                      <TableHead scope="col" className="text-right">
                        Placed
                      </TableHead>
                      <TableHead scope="col" className="text-right">
                        Not graded
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {run.scenarios.map((row) => (
                      <TableRow key={row.scenario}>
                        <TableCell className="font-medium">{row.scenario}</TableCell>
                        <TableCell>
                          {formatRate(row)}
                          <span className="ml-2 text-xs text-muted-foreground">
                            {formatInterval(row)}
                          </span>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{row.attempted}</TableCell>
                        <TableCell className="text-right tabular-nums">{row.not_graded}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Failures, by fault class</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {run.failures.length === 0 ? (
                <p className="text-sm text-muted-foreground">No graded call failed.</p>
              ) : (
                run.failures.map((group) => (
                  <section key={group.fault_class} aria-label={`Fault class ${group.fault_class}`}>
                    <h3 className="mb-2 text-sm font-semibold">
                      {group.fault_class}{" "}
                      <span className="font-normal text-muted-foreground">
                        ({group.calls.length})
                      </span>
                    </h3>
                    <ul className="space-y-2">
                      {group.calls.map((call) => (
                        <li key={call.id} className="rounded-md border p-3 text-sm">
                          <Link
                            href={`/dashboard/caller-runs/${run.id}/calls/${call.id}`}
                            className={linkClass}
                          >
                            Call {call.position}: {call.scenario}
                          </Link>
                          {call.last_step && (
                            <p className="mt-1 text-muted-foreground">
                              Stopped at: {call.last_step}
                            </p>
                          )}
                          {call.failed_checks.map((check) => (
                            <p key={check} className="mt-1">
                              Failed check: {check}
                            </p>
                          ))}
                        </li>
                      ))}
                    </ul>
                  </section>
                ))
              )}
            </CardContent>
          </Card>

          <FieldsTable run={run} />

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Left out of the pass rate</CardTitle>
            </CardHeader>
            <CardContent>
              {run.excluded.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  Nothing was left out: every call in this run was placed and graded.
                </p>
              ) : (
                <Table>
                  <caption className="sr-only">
                    Calls that are not counted in the pass rate, with the reason for each
                  </caption>
                  <TableHeader>
                    <TableRow>
                      <TableHead scope="col">Scenario</TableHead>
                      <TableHead scope="col">Outcome</TableHead>
                      <TableHead scope="col">Reason</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {run.excluded.map((item, index) => (
                      <TableRow key={`${item.call_id ?? "not-run"}-${index}`}>
                        <TableCell className="font-medium">
                          {item.call_id ? (
                            <Link
                              href={`/dashboard/caller-runs/${run.id}/calls/${item.call_id}`}
                              className={linkClass}
                            >
                              {item.scenario || item.scenario_file}
                            </Link>
                          ) : (
                            item.scenario || item.scenario_file
                          )}
                        </TableCell>
                        <TableCell>
                          <OutcomeBadge outcome={item.outcome} />
                          {item.fault && (
                            <span className="ml-2 text-xs text-muted-foreground">
                              fault: {item.fault}
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="text-sm">
                          {item.reason || "No reason given"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Every call</CardTitle>
            </CardHeader>
            <CardContent>
              {run.calls.length === 0 ? (
                <p className="text-sm text-muted-foreground">No call was placed in this run.</p>
              ) : (
                <Table>
                  <caption className="sr-only">Every call placed in this run, in order</caption>
                  <TableHeader>
                    <TableRow>
                      <TableHead scope="col">Call</TableHead>
                      <TableHead scope="col">Outcome</TableHead>
                      <TableHead scope="col">Fault class</TableHead>
                      <TableHead scope="col">Stopped at</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {run.calls.map((call) => (
                      <TableRow key={call.id}>
                        <TableCell>
                          <Link
                            href={`/dashboard/caller-runs/${run.id}/calls/${call.id}`}
                            className={linkClass}
                          >
                            {call.position}. {call.scenario}
                          </Link>
                          {call.synthetic && (
                            <span className="ml-2 text-xs text-muted-foreground">
                              synthetic voice
                            </span>
                          )}
                          {call.labels_unverified && (
                            <span className="ml-2 text-xs text-muted-foreground">
                              labels unverified
                            </span>
                          )}
                        </TableCell>
                        <TableCell>
                          <OutcomeBadge outcome={call.outcome} />
                        </TableCell>
                        <TableCell className="text-sm">{call.fault_class ?? "-"}</TableCell>
                        <TableCell className="max-w-md text-sm text-muted-foreground">
                          {call.last_step || "-"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
