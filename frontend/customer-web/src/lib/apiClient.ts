import type { ApiErrorBody } from './types';

/**
 * Error thrown by apiFetch. Carries the machine-readable `code` and
 * human-readable `message` from the standard error envelope so the UI
 * (QueryBoundary) can render both.
 */
export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

/** Paths where a 401 is an expected response (bad creds), not a dead session. */
function isAuthPath(path: string): boolean {
  return path.startsWith('/v1/auth/');
}

/**
 * Shared fetch wrapper.
 * - credentials: 'include' so the httpOnly session cookie travels.
 * - JSON Content-Type by default.
 * - On a 401 we boot to /login, EXCEPT:
 *     - when the request is an auth endpoint (login itself returns 401 on bad
 *       creds — the form must surface that inline), or
 *     - when we are already on /login (avoid a redirect loop / swallowed error).
 *   In those cases we throw an ApiError(401) so the caller can show it inline.
 */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: 'include',
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });

  if (res.status === 401) {
    const onLoginPage = window.location.pathname === '/login';
    if (!isAuthPath(path) && !onLoginPage) {
      window.location.assign('/login');
      throw new ApiError(401, 'unauthenticated', 'Your session has expired.');
    }
  }

  // 204 No Content (e.g. logout) — nothing to parse.
  if (res.status === 204) {
    return undefined as T;
  }

  let body: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!res.ok) {
    const err = body as ApiErrorBody | null;
    const code = err?.error?.code ?? 'unknown_error';
    const message =
      err?.error?.message ?? `Request failed with status ${res.status}.`;
    throw new ApiError(res.status, code, message);
  }

  return body as T;
}
