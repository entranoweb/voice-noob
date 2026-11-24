import { NextResponse, type NextRequest } from "next/server";

import { frameAncestorsFor } from "@/lib/embed-frame-policy";

/**
 * Sends the widget page with a per-agent `frame-ancestors` policy.
 *
 * Only the widget page itself is matched. The preview page next to it is this
 * deployment's own dashboard page and frames the widget from the same origin,
 * which `'self'` always allows.
 */
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const publicId = request.nextUrl.pathname.split("/")[2] ?? "";
  const response = NextResponse.next();
  response.headers.set(
    "Content-Security-Policy",
    `frame-ancestors ${await frameAncestorsFor(publicId)}`
  );
  return response;
}

export const config = {
  matcher: "/embed/:publicId",
};
