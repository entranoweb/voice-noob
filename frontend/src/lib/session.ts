/**
 * Where the browser keeps the session, and how it renews it.
 *
 * The access token lives about half an hour. The refresh token renews it and
 * works once: each renewal returns a new pair and retires the old refresh
 * token. Both sit in localStorage, as the access token always has. Both are sent
 * as a Bearer header or in a JSON body, never as a cookie, so there is no
 * ambient credential for a cross-site request to ride on and no CSRF surface.
 * The cost is the same as for the access token before: a script that runs on
 * the page (XSS) can read them. An httpOnly cookie would close that and would
 * bring CSRF protection with it; it is a deliberate follow-up, not done here.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const ACCESS_TOKEN_KEY = "access_token";
export const REFRESH_TOKEN_KEY = "refresh_token";

function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch (error) {
    console.warn(`Failed to store "${key}":`, error);
  }
}

function safeRemove(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    // Nothing to clear if storage is unavailable.
  }
}

export function getAccessToken(): string | null {
  return safeGet(ACCESS_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  return safeGet(REFRESH_TOKEN_KEY);
}

export function storeSession(tokens: { access_token: string; refresh_token?: string | null }) {
  safeSet(ACCESS_TOKEN_KEY, tokens.access_token);
  if (tokens.refresh_token) {
    safeSet(REFRESH_TOKEN_KEY, tokens.refresh_token);
  }
}

export function clearSession(): void {
  safeRemove(ACCESS_TOKEN_KEY);
  safeRemove(REFRESH_TOKEN_KEY);
}

/** Tell the server to end this sign-in. Best effort: the local session is cleared either way. */
export async function endSessionOnServer(): Promise<void> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return;
  try {
    await fetch(`${API_BASE}/api/v1/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
  } catch {
    // Offline: the refresh token expires on its own.
  }
}

let inFlight: Promise<string | null> | null = null;

const LOCK_NAME = "synthiq-session-refresh";

/**
 * Run ``task`` while holding a lock shared by every tab of this origin
 * (Web Locks). Where the API is missing the task just runs: that browser falls
 * back to the per-tab guard in ``refreshSession`` and the small chance that two
 * tabs collide stays.
 */
async function withCrossTabLock<T>(task: () => Promise<T>): Promise<T> {
  const locks = typeof navigator === "undefined" ? undefined : navigator.locks;
  if (!locks || typeof locks.request !== "function") return task();
  return await locks.request(LOCK_NAME, task);
}

/**
 * Trade the refresh token for a new access token. Returns it, or null when the
 * session is over (no refresh token, or the server refused it).
 *
 * A refresh token works once, and a second use looks like theft to the server
 * and ends the sign-in everywhere. Two guards keep that from happening to a
 * person with several tabs open:
 *
 * - within a tab, concurrent callers share one promise (``inFlight``);
 * - across tabs, the call is made while holding a Web Lock, and once the lock is
 *   ours the stored refresh token is read again. If it is no longer the one this
 *   call set out with, another tab already rotated it, so its stored pair is
 *   used and /refresh is not called.
 */
export function refreshSession(): Promise<string | null> {
  if (inFlight) return inFlight;
  const startedWith = getRefreshToken();
  if (!startedWith) return Promise.resolve(null);

  inFlight = withCrossTabLock(async () => {
    const current = getRefreshToken();
    if (!current) return null;
    if (current !== startedWith) return getAccessToken();
    try {
      const response = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: current }),
      });
      if (!response.ok) {
        if (response.status === 401) clearSession();
        return null;
      }
      const data = (await response.json()) as { access_token: string; refresh_token?: string };
      storeSession(data);
      return data.access_token;
    } catch {
      // A network error is not a verdict on the session: keep it and try later.
      return null;
    }
  }).finally(() => {
    inFlight = null;
  });
  return inFlight;
}

/** When an access token expires (ms since epoch), read from its payload; null if unreadable. */
export function accessTokenExpiry(token: string): number | null {
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    const exp = (JSON.parse(json) as { exp?: unknown }).exp;
    return typeof exp === "number" ? exp * 1000 : null;
  } catch {
    return null;
  }
}

/** Renew this long before the access token runs out. */
export const RENEW_MARGIN_MS = 2 * 60 * 1000;
/** Renewal fires up to this much earlier at random, so open tabs do not all renew at once. */ export const RENEW_JITTER_MS =
  30 * 1000;

/**
 * `fetch` for authenticated calls: sends the current access token, and when the
 * server answers 401, renews the session once and sends the request again. If
 * the renewal fails the 401 is returned as it is, and `refreshSession` has
 * already cleared the session. Bodies must be strings or other replayable values.
 */
export async function authedFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const send = (token: string | null) => {
    const headers = new Headers(init.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return fetch(input, { ...init, headers });
  };

  const response = await send(getAccessToken());
  if (response.status !== 401) return response;

  const renewed = await refreshSession();
  return renewed ? send(renewed) : response;
}
