/**
 * Which sites may put an agent's widget page in a frame.
 *
 * The backend owns the answer (it holds each agent's allowed sites). This
 * module asks it, on the server, when the widget page is served, so the page
 * can go out with a `Content-Security-Policy: frame-ancestors ...` header and
 * the browser itself refuses to draw the widget inside a site the agent's owner
 * did not list.
 *
 * It fails closed. If the backend cannot be reached, is slow, or answers with
 * anything unexpected, the policy is `'self'`: only this deployment's own pages
 * (the dashboard preview) may frame the widget until the backend answers again.
 */

export const FRAME_ANCESTORS_SELF = "'self'";

const CACHE_SECONDS = 60;
const FAILURE_CACHE_SECONDS = 5;
const TIMEOUT_MS = 2000;

// The value goes into a response header. Whatever the backend says, only
// source expressions get through: 'self', *, and scheme://host[:port|:*].
const SOURCE =
  /^(?:'self'|\*|https?:\/\/(?:\*\.)?[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::(?:\d{1,5}|\*))?)$/;
const PUBLIC_ID = /^[A-Za-z0-9_-]{1,64}$/;

type Entry = { value: string; expires: number };
const cache = new Map<string, Entry>();

export function clearFramePolicyCache(): void {
  cache.clear();
}

/** The backend's answer if every part of it is a source expression, else null. */
export function safeFrameAncestors(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const sources = value.trim().split(/\s+/).filter(Boolean);
  if (sources.length === 0 || !sources.every((source) => SOURCE.test(source))) return null;
  return sources.join(" ");
}

export function apiBaseUrl(): string {
  return (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
}

export async function frameAncestorsFor(
  publicId: string,
  fetchImpl: typeof fetch = fetch,
  now: () => number = Date.now
): Promise<string> {
  if (!PUBLIC_ID.test(publicId)) return FRAME_ANCESTORS_SELF;

  const cached = cache.get(publicId);
  if (cached && cached.expires > now()) return cached.value;

  let value: string | null = null;
  try {
    const response = await fetchImpl(
      `${apiBaseUrl()}/api/public/embed/${encodeURIComponent(publicId)}/frame-policy`,
      { cache: "no-store", signal: AbortSignal.timeout(TIMEOUT_MS) }
    );
    if (response.ok) {
      const body = (await response.json()) as { frame_ancestors?: unknown };
      value = safeFrameAncestors(body.frame_ancestors);
    }
  } catch {
    value = null;
  }

  // A failure is remembered only briefly, so one bad moment does not block an
  // agent's embeds for a minute, and a dead backend is not asked on every hit.
  const seconds = value === null ? FAILURE_CACHE_SECONDS : CACHE_SECONDS;
  const result = value ?? FRAME_ANCESTORS_SELF;
  cache.set(publicId, { value: result, expires: now() + seconds * 1000 });
  return result;
}
