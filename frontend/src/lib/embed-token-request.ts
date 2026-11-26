/**
 * Headers for the widget's `POST /embed/{id}/token` call.
 *
 * A widget that still holds a live session token sends it back as
 * `X-Embed-Session`; the server refuses a second key for that session (409). A
 * client that leaves the header off is not stopped by this: it is limited only by
 * the key's short expiry and the per-agent and per-IP hourly caps. The widget
 * forgets its token when a session ends, so a restart is a new session.
 */
export function embedTokenHeaders(
  sessionToken: string,
  origin: string,
  parentHeaders: Record<string, string>
): Record<string, string> {
  return {
    "Content-Type": "application/json",
    Origin: origin,
    ...(sessionToken ? { "X-Embed-Session": sessionToken } : {}),
    ...parentHeaders,
  };
}
