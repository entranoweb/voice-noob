"use client";

import Link from "next/link";
import { useRef } from "react";
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
import { CallAudio, type AudioMarker, type CallAudioHandle } from "@/components/caller/call-audio";
import { ErrorState, LoadingState } from "@/components/caller/page-state";
import {
  errorMessage,
  getCallerCall,
  type CallerCallDetail,
  type SaidEntry,
  type TimelineEvent,
  type ToolCall,
} from "@/lib/api/caller-runs";

interface Line {
  t: number | null;
  who: string;
  text: string;
  detail?: string;
  /** What this row is on the recording, when it is one of the marked kinds. */
  marker?: AudioMarker["kind"];
}

function compact(value: unknown): string {
  if (value === undefined || value === null) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 400 ? `${text.slice(0, 400)}...` : text;
}

/** What the caller meant to say at a "said" event: the scripted line for that moment. */
function intendedAt(event: TimelineEvent, said: SaidEntry[]): string {
  const match = said.find((entry) => entry.t === event.t);
  return match?.intended ?? "";
}

function eventLine(event: TimelineEvent, said: SaidEntry[]): Line | null {
  const t = typeof event.t === "number" ? event.t : null;
  switch (event.kind) {
    case "agent":
      return { t, who: "Agent said", text: event.text ?? "", marker: "agent" };
    case "said": {
      const clips = Array.isArray(event.clips) ? event.clips.join(", ") : "";
      return {
        t,
        who: "Caller said",
        text: intendedAt(event, said) || "(recorded clip)",
        detail: clips ? `clip: ${clips}` : undefined,
        marker: "caller",
      };
    }
    case "heard":
      return { t, who: "Agent heard", text: event.text ?? "" };
    case "dtmf":
      return { t, who: "Caller pressed", text: String(event.digits ?? "") };
    case "silent":
      return { t, who: "Caller", text: "stayed silent" };
    case "interrupted":
      return { t, who: "Agent", text: "was interrupted" };
    case "hangup":
      return { t, who: "Caller", text: `hung up${event.on ? ` (${event.on})` : ""}` };
    case "closed":
      return { t, who: "Call", text: `ended${event.reason ? ` (${event.reason})` : ""}` };
    case "error":
      return { t, who: "Rig error", text: event.text ?? "" };
    case "no_answer_needed":
      return null;
    default:
      // An event kind this page has not seen: show it rather than drop it.
      return { t, who: event.kind ?? "event", text: compact({ ...event, t: undefined }) };
  }
}

/** What the tool reported back: its named outputs when it has them, else its raw result. */
function toolOutcome(call: ToolCall): string {
  if (call.error) return `error: ${call.error}`;
  const outputs = call.outputs && Object.keys(call.outputs).length > 0 ? call.outputs : null;
  if (outputs) return `reported: ${compact(outputs)}`;
  return `result: ${compact(call.result) || "none"}`;
}

/** The platform withheld this tool's result: not checked, and not a refusal by the agent. */
function isUnavailable(call: ToolCall): boolean {
  const args = call.arguments;
  return (
    call.result === "unavailable" ||
    (typeof args === "object" && args !== null && "_unavailable" in args)
  );
}

function toolLine(call: ToolCall): Line {
  return {
    t: typeof call.t === "number" ? call.t : null,
    who: isUnavailable(call)
      ? "Tool call, result unavailable"
      : call.ok === false
        ? "Tool call refused"
        : "Tool call",
    text: `${call.name ?? "?"}(${compact(call.arguments)})`,
    detail: toolOutcome(call),
    marker: "tool",
  };
}

function buildTimeline(call: CallerCallDetail): Line[] {
  const lines = [
    ...call.timeline.map((event) => eventLine(event, call.said)),
    ...call.tool_calls.map(toolLine),
  ].filter((line): line is Line => line !== null);
  // Tool calls with no time go last, in the order the agent made them.
  return lines
    .map((line, index) => ({ line, index }))
    .sort((a, b) => {
      const left = a.line.t ?? Number.POSITIVE_INFINITY;
      const right = b.line.t ?? Number.POSITIVE_INFINITY;
      return left === right ? a.index - b.index : left - right;
    })
    .map(({ line }) => line);
}

function shown(value: string | null | undefined): string {
  return value === null || value === undefined || value === "" ? "-" : value;
}

export default function CallerCallPage() {
  const params = useParams<{ runId: string; callId: string }>();
  const { runId, callId } = params;
  const {
    data: call,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["caller-call", runId, callId],
    queryFn: () => getCallerCall(runId, callId),
    enabled: Boolean(runId && callId),
  });

  const lines = call ? buildTimeline(call) : [];
  const player = useRef<CallAudioHandle>(null);
  const audio = call?.audio ?? [];
  const markers: AudioMarker[] = lines.flatMap((line) =>
    line.marker && line.t !== null
      ? [{ t: line.t, kind: line.marker, label: `${line.who}: ${line.text}` }]
      : []
  );

  return (
    <div className="space-y-4">
      <Link
        href={`/dashboard/caller-runs/${runId}`}
        className="inline-flex items-center text-sm font-medium underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ChevronLeft className="mr-1 h-4 w-4" aria-hidden />
        Back to the run
      </Link>

      {isLoading ? (
        <LoadingState label="Loading call..." />
      ) : error || !call ? (
        <ErrorState
          title="Failed to load this call"
          message={errorMessage(error, "The server did not answer.")}
        />
      ) : (
        <>
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-xl font-semibold">
                Call {call.position}: {call.scenario}
              </h1>
              <OutcomeBadge outcome={call.outcome} />
            </div>
            <p className="text-sm text-muted-foreground">
              {call.run.target_label || call.run.run_id}
              {call.duration_s !== null ? `, ${Math.round(call.duration_s)} seconds` : ""}
              {call.ended ? `, ${call.ended}` : ""}
              {call.fault_class ? `, fault class: ${call.fault_class}` : ""}
            </p>
            {call.conversation_id && (
              <p className="font-mono text-xs text-muted-foreground">
                Conversation {call.conversation_id}
              </p>
            )}
          </div>

          {call.excluded_reason && (
            <Card>
              <CardContent className="pt-6 text-sm">
                <p className="font-medium">This call is not counted in the pass rate.</p>
                <p className="text-muted-foreground">{call.excluded_reason}</p>
              </CardContent>
            </Card>
          )}

          {(call.synthetic_clips.length > 0 || call.labels_unverified.length > 0) && (
            <Card>
              <CardContent
                className="space-y-1 pt-6 text-sm"
                role="note"
                aria-label="Clip provenance"
              >
                {call.synthetic_clips.length > 0 && (
                  <p>
                    <span className="font-medium">Synthetic voice:</span> these clips are a
                    synthetic voice, not a recorded caller: {call.synthetic_clips.join(", ")}
                  </p>
                )}
                {call.labels_unverified.length > 0 && (
                  <p>
                    <span className="font-medium">Labels unverified:</span> no person has confirmed
                    what these clips say: {call.labels_unverified.join(", ")}
                  </p>
                )}
              </CardContent>
            </Card>
          )}

          {Object.entries(call.notes).length > 0 && (
            <Card>
              <CardContent className="space-y-1 pt-6 text-sm">
                {Object.entries(call.notes).map(([key, value]) => (
                  <p key={key}>
                    <span className="font-medium">{key.replaceAll("_", " ")}:</span> {value}
                  </p>
                ))}
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Said, heard, stored</CardTitle>
            </CardHeader>
            <CardContent>
              {call.fields.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  The caller gave no graded detail in this call.
                </p>
              ) : (
                <Table>
                  <caption className="sr-only">
                    For each detail the caller gave: what was said, what the agent heard, and what
                    was stored
                  </caption>
                  <TableHeader>
                    <TableRow>
                      <TableHead scope="col">Field</TableHead>
                      <TableHead scope="col">Said</TableHead>
                      <TableHead scope="col">Heard</TableHead>
                      <TableHead scope="col">Stored</TableHead>
                      <TableHead scope="col">Stored from</TableHead>
                      <TableHead scope="col">Verdict</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {call.fields.map((row, index) => (
                      <TableRow key={`${row.field}-${index}`}>
                        <TableCell className="font-medium">{row.field}</TableCell>
                        <TableCell>
                          {shown(row.said)}
                          {row.synthetic && (
                            <span className="ml-2 text-xs text-muted-foreground">
                              synthetic voice
                            </span>
                          )}
                          {row.labels_unverified && (
                            <span className="ml-2 text-xs text-muted-foreground">
                              label unverified
                            </span>
                          )}
                        </TableCell>
                        <TableCell>
                          {shown(row.heard)}
                          {row.heard_matches === false && (
                            <span className="ml-2 text-xs font-medium text-red-700 dark:text-red-400">
                              misheard
                            </span>
                          )}
                        </TableCell>
                        <TableCell>
                          {shown(row.stored)}
                          {row.stored_matches === false && (
                            <span className="ml-2 text-xs font-medium text-red-700 dark:text-red-400">
                              wrong
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {shown(row.stored_source)}
                        </TableCell>
                        <TableCell className="text-sm">{shown(row.verdict)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
              {call.fields.some((row) => row.stored_source === "tool call") && (
                <p className="mt-3 text-xs text-muted-foreground">
                  &quot;tool call&quot; means the value is what the agent&apos;s tool was sent, not
                  a record read back from the system afterwards: sent, not confirmed stored.
                </p>
              )}
            </CardContent>
          </Card>

          {call.checks.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Checks</CardTitle>
              </CardHeader>
              <CardContent>
                <ul className="space-y-1 text-sm">
                  {call.checks.map((check, index) => (
                    <li key={`${check.what}-${index}`} className="flex items-start gap-2">
                      <OutcomeBadge outcome={check.passed ? "passed" : "failed"} />
                      <span>{check.what}</span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}

          <CallAudio
            ref={player}
            audio={audio}
            markers={markers}
            expired={call?.audio_expired ?? false}
          />

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Timeline</CardTitle>
            </CardHeader>
            <CardContent>
              {lines.length === 0 ? (
                <p className="text-sm text-muted-foreground">Nothing was recorded for this call.</p>
              ) : (
                <ol aria-label="Call timeline" className="space-y-2">
                  {lines.map((line, index) => (
                    <li
                      key={index}
                      className="grid grid-cols-[4rem_9rem_1fr] gap-3 border-b pb-2 text-sm last:border-0"
                    >
                      <span className="tabular-nums text-muted-foreground">
                        {line.t === null ? (
                          ""
                        ) : audio.length > 0 ? (
                          <button
                            type="button"
                            className="underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            aria-label={`Play from ${line.t.toFixed(1)} seconds: ${line.who}`}
                            onClick={() => player.current?.seek(line.t as number)}
                          >
                            {`${line.t.toFixed(1)}s`}
                          </button>
                        ) : (
                          `${line.t.toFixed(1)}s`
                        )}
                      </span>
                      <span className="font-medium">{line.who}</span>
                      <span className="min-w-0 break-words">
                        {line.text}
                        {line.detail && (
                          <span className="block font-mono text-xs text-muted-foreground">
                            {line.detail}
                          </span>
                        )}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
              {call.step_path.length > 0 && (
                <p className="mt-3 text-xs text-muted-foreground">
                  Agent steps: {call.step_path.join(" > ")}
                </p>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
