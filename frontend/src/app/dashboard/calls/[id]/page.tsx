"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, ArrowLeft, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { getCall, getCallMetrics, type CallMetricScore } from "@/lib/api/calls";

function formatDuration(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins === 0) return `${secs}s`;
  return `${mins}m ${secs}s`;
}

function formatWhen(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "Not recorded";
}

/**
 * A metric with no value was not measurable for this call (no audio, or nothing
 * to compare against). That is different from a measured zero, and the two must
 * not be shown the same way.
 */
function formatMetricValue(score: CallMetricScore): string {
  if (score.value === null) return "Not measurable";
  const rounded = Number.isInteger(score.value) ? score.value.toString() : score.value.toFixed(2);
  return score.unit ? `${rounded} ${score.unit}` : rounded;
}

function metricVerdict(score: CallMetricScore): string {
  if (score.passed === null) return "No verdict";
  return score.passed ? "Pass" : "Fail";
}

export default function CallDetailPage() {
  const params = useParams<{ id: string }>();
  const callId = params.id;

  const {
    data: call,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["call", callId],
    queryFn: () => getCall(callId),
    enabled: Boolean(callId),
  });

  const { data: metrics, error: metricsError } = useQuery({
    queryKey: ["call-metrics", callId],
    queryFn: () => getCallMetrics(callId),
    enabled: Boolean(call),
  });

  return (
    <div className="space-y-4">
      <Link
        href="/dashboard/calls"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Call history
      </Link>

      {isLoading && (
        <div className="flex items-center gap-2 py-16 text-muted-foreground" role="status">
          <Loader2 className="h-5 w-5 animate-spin" />
          Loading call...
        </div>
      )}

      {error instanceof Error && (
        <div role="alert" className="flex items-start gap-2 rounded-md border p-4">
          <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
          <div>
            <h1 className="text-lg font-semibold">This call could not be loaded</h1>
            <p className="text-sm text-muted-foreground">{error.message}</p>
          </div>
        </div>
      )}

      {call && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h1 className="text-xl font-semibold">
                Call with {call.contact_name ?? call.from_number}
              </h1>
              <p className="text-sm text-muted-foreground">
                {call.agent_name ?? "Unknown agent"} &middot; {formatWhen(call.started_at)}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant="outline">{call.direction}</Badge>
              <Badge variant="secondary">{call.status.replace("_", " ")}</Badge>
            </div>
          </div>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Details</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <dt className="text-xs text-muted-foreground">From</dt>
                  <dd className="font-mono">{call.from_number}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">To</dt>
                  <dd className="font-mono">{call.to_number}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Duration</dt>
                  <dd>{formatDuration(call.duration_seconds)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Answered</dt>
                  <dd>{formatWhen(call.answered_at)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Ended</dt>
                  <dd>{formatWhen(call.ended_at)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Workspace</dt>
                  <dd>{call.workspace_name ?? "None"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Carrier</dt>
                  <dd>{call.provider}</dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-xs text-muted-foreground">Carrier call reference</dt>
                  <dd className="break-all font-mono text-xs">{call.provider_call_id}</dd>
                </div>
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Recording</CardTitle>
            </CardHeader>
            <CardContent>
              {call.recording_url ? (
                <audio controls preload="none" src={call.recording_url} className="w-full">
                  <a href={call.recording_url}>Download the recording</a>
                </audio>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No recording was kept for this call.
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Measured on this call</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {metricsError instanceof Error && (
                <p role="alert" className="text-sm text-destructive">
                  The measurements could not be loaded: {metricsError.message}
                </p>
              )}
              {metrics && (
                <>
                  <p className="text-sm text-muted-foreground">
                    {metrics.has_audio
                      ? "Timings were captured from the live call."
                      : "No audio timings were captured for this call, so most rows cannot be measured."}{" "}
                    {!metrics.trustworthy &&
                      "This set of measurements is flagged as not trustworthy; see the reasons below."}
                  </p>
                  {metrics.invalid_reasons.length > 0 && (
                    <ul className="list-disc pl-5 text-sm text-muted-foreground">
                      {metrics.invalid_reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  )}
                  {metrics.scores.length === 0 ? (
                    <p className="text-sm text-muted-foreground">Nothing was measured.</p>
                  ) : (
                    <div className="overflow-x-auto">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Measure</TableHead>
                            <TableHead>Group</TableHead>
                            <TableHead>Value</TableHead>
                            <TableHead>Verdict</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {metrics.scores.map((score) => (
                            <TableRow key={`${score.metric}-${score.version}`}>
                              <TableCell className="font-medium">
                                {score.metric.replace(/_/g, " ")}
                              </TableCell>
                              <TableCell className="text-muted-foreground">
                                {score.category}
                              </TableCell>
                              <TableCell
                                className={score.value === null ? "text-muted-foreground" : ""}
                              >
                                {formatMetricValue(score)}
                              </TableCell>
                              <TableCell>{metricVerdict(score)}</TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  )}
                </>
              )}
              {!metrics && !metricsError && (
                <p className="text-sm text-muted-foreground" role="status">
                  Loading measurements...
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Transcript</CardTitle>
            </CardHeader>
            <CardContent>
              {call.transcript ? (
                <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 p-3 text-sm">
                  {call.transcript}
                </pre>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No transcript was stored for this call.
                </p>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
