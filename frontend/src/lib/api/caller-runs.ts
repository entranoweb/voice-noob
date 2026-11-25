/**
 * API client for caller runs: batches of test calls uploaded from the caller rig.
 *
 * The result format is still moving, so the list-like parts (fields, timeline
 * events, tool calls) are typed with the keys the pages read and left open for
 * the rest. A page must cope with a key being absent.
 */

import { api } from "@/lib/api";

/** A pass count over a total. `rate` is null when there was nothing to rate. */
export interface Rate {
  passed: number;
  total: number;
  rate: number | null;
  interval_low: number | null;
  interval_high: number | null;
}

export interface CallerRunSummary {
  id: string;
  run_id: string;
  workspace_id: string;
  workspace_name: string | null;
  target_label: string;
  rig_version: string;
  scenario_set_hash: string;
  payload_version: number;
  started_at: string | null;
  ended_at: string | null;
  uploaded_at: string;
  attempted: number;
  graded: number;
  passed: number;
  failed: number;
  not_graded: number;
  not_run: number;
  excluded_count: number;
  /**
   * Calls by outcome as the caller wrote it (passed, failed, void, ungradeable,
   * ungraded, or one this page has not met), with `synthetic_calls`,
   * `labels_unverified_calls`, `unavailable_calls` and `rehearsal_calls` beside them.
   */
  outcome_counts: Record<string, number>;
  /** Placed against a scripted stand-in (`batch --leg fake`), not an agent. */
  rehearsal: boolean;
  of_graded: Rate;
  of_attempted: Rate;
}

export interface CallerRunListResponse {
  runs: CallerRunSummary[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface ScenarioRow extends Rate {
  scenario: string;
  attempted: number;
  graded: number;
  failed: number;
  not_graded: number;
}

export interface FailureGroup {
  fault_class: string;
  calls: {
    id: string;
    position: number;
    scenario: string;
    last_step: string;
    failed_checks: string[];
  }[];
}

export interface ExcludedCall {
  kind: string;
  call_id: string | null;
  scenario: string;
  scenario_file: string;
  repeat: number | null;
  outcome: string;
  fault?: string;
  reason: string;
}

export interface FieldTally {
  field: string;
  answered: number;
  stored_correctly: number;
  /** The tool was sent the right value; nothing confirmed the system kept it. */
  sent_correctly: number;
  misheard: number;
  mis_stored: number;
  not_stored: number;
  read_back: number;
  labels_unverified: number;
  synthetic: number;
}

export interface CallerCallRow {
  id: string;
  position: number;
  scenario: string;
  outcome: string;
  fault_class: string | null;
  last_step: string;
  duration_s: number | null;
  synthetic: boolean;
  labels_unverified: boolean;
}

export interface CallerRunDetail extends CallerRunSummary {
  scenarios: ScenarioRow[];
  failures: FailureGroup[];
  excluded: ExcludedCall[];
  chains: Record<string, unknown>[];
  fields: FieldTally[];
  calls: CallerCallRow[];
  reported_summary: Record<string, unknown> | null;
}

export interface FieldRow {
  field: string;
  said?: string | null;
  heard?: string | null;
  stored?: string | null;
  heard_matches?: boolean | null;
  stored_matches?: boolean | null;
  verdict?: string;
  stored_source?: string;
  labels_unverified?: boolean;
  synthetic?: boolean;
  [key: string]: unknown;
}

export interface TimelineEvent {
  t?: number;
  kind?: string;
  text?: string;
  clips?: string[];
  digits?: string;
  reason?: string;
  on?: string;
  [key: string]: unknown;
}

export interface ToolCall {
  name?: string;
  arguments?: unknown;
  result?: unknown;
  /** Named values the tool reported back, e.g. found / not found / near match. */
  outputs?: Record<string, unknown>;
  ok?: boolean;
  step_id?: string;
  t?: number | null;
  error?: string;
  [key: string]: unknown;
}

export interface SaidEntry {
  t?: number;
  field?: string;
  intended?: string;
  clips?: string[];
  [key: string]: unknown;
}

export interface CallerCallDetail {
  id: string;
  run: { id: string; run_id: string; target_label: string };
  position: number;
  file: string;
  scenario: string;
  scenario_file: string;
  mutates: string;
  repeat: number | null;
  outcome: string;
  fault_class: string | null;
  last_step: string;
  conversation_id: string | null;
  ended: string;
  duration_s: number | null;
  excluded_reason: string | null;
  fields: FieldRow[];
  checks: { what?: string; passed?: boolean }[];
  timeline: TimelineEvent[];
  tool_calls: ToolCall[];
  said: SaidEntry[];
  step_path: string[];
  read_back: Record<string, unknown> | null;
  synthetic_clips: string[];
  labels_unverified: string[];
  notes: Record<string, string>;
}

export async function listCallerRuns(
  params: { page?: number; page_size?: number; workspace_id?: string } = {}
): Promise<CallerRunListResponse> {
  const response = await api.get<CallerRunListResponse>("/api/v1/caller/runs", { params });
  return response.data;
}

export async function getCallerRun(runId: string): Promise<CallerRunDetail> {
  const response = await api.get<CallerRunDetail>(`/api/v1/caller/runs/${runId}`);
  return response.data;
}

export async function getCallerCall(runId: string, callId: string): Promise<CallerCallDetail> {
  const response = await api.get<CallerCallDetail>(`/api/v1/caller/runs/${runId}/calls/${callId}`);
  return response.data;
}

/**
 * A fraction as a percentage, cut down to one decimal place and never rounded
 * up: 199 of 200 is "99.5%", not "100%". A whole number shows without ".0".
 */
export function percent(value: number): string {
  const tenths = Math.floor(value * 1000 + 1e-9) / 10;
  return `${Number.isInteger(tenths) ? tenths : tenths.toFixed(1)}%`;
}

/** "3 of 4 (75%)", or "none graded" when there is nothing to rate. */
export function formatRate(rate: Rate, nothing = "none graded"): string {
  if (rate.rate === null || rate.total === 0) return nothing;
  return `${rate.passed} of ${rate.total} (${percent(rate.rate)})`;
}

/** "95% interval 30% to 95%", or an empty string when there is no interval. */
export function formatInterval(rate: Rate): string {
  if (rate.interval_low === null || rate.interval_high === null) return "";
  return `95% interval ${percent(rate.interval_low)} to ${percent(rate.interval_high)}`;
}

export function errorMessage(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null) {
    const status = (error as { response?: { status?: number } }).response?.status;
    if (status === 404) return "Not found. It may have been deleted, or belong to another account.";
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string" && message) return message;
  }
  return fallback;
}
