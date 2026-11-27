/**
 * Response headers for every page except the embeddable widget.
 *
 * The widget (`/embed/:publicId`) is the one page customer sites frame, and it
 * gets its own per-agent `frame-ancestors` policy from `src/proxy.ts`. This
 * list must not match it: a blanket `X-Frame-Options: DENY` or
 * `frame-ancestors 'none'` there would blank every embedded widget, and two
 * differing frame policies on one response resolve to the stricter.
 */

export type HeaderRule = { source: string; headers: { key: string; value: string }[] };

// Every path except /embed and /embed/... ("embed" must be a whole segment).
export const NON_EMBED_SOURCE = "/((?!embed(?:/|$)).*)";

const HSTS = "max-age=31536000";

export function securityHeaders(production: boolean): HeaderRule[] {
  const headers = [
    { key: "X-Frame-Options", value: "DENY" },
    { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    { key: "Permissions-Policy", value: "geolocation=(), payment=()" },
  ];
  // Browsers ignore HSTS over plain http, so this is harmless on a developer's
  // machine, but it is left out of development builds so a localhost run never
  // pins anything.
  if (production) headers.push({ key: "Strict-Transport-Security", value: HSTS });
  return [{ source: NON_EMBED_SOURCE, headers }];
}
