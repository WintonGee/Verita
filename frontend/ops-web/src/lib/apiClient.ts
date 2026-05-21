import type { ApiErrorBody } from '../types';

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

/**
 * Read a cookie value by name. Read fresh on every request because the
 * csrftoken cookie is not set until GET /ops/auth/me runs after page load.
 */
export function getCookie(name: string): string {
  const prefix = name + '=';
  const parts = document.cookie ? document.cookie.split('; ') : [];
  for (const part of parts) {
    if (part.startsWith(prefix)) {
      return decodeURIComponent(part.slice(prefix.length));
    }
  }
  return '';
}

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

function isLoginPath(path: string): boolean {
  return path === '/ops/auth/login';
}

/**
 * Shared fetch wrapper:
 * - same-origin, credentials included (session cookie)
 * - JSON content-type
 * - attaches X-CSRFToken on unsafe methods (read fresh each call)
 * - 401 -> redirect to /login (except while already on /login or hitting login)
 * - parses the standard error envelope into an ApiError
 */
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();

  const headers = new Headers(init.headers);
  if (!headers.has('Content-Type') && init.body) {
    headers.set('Content-Type', 'application/json');
  }
  if (UNSAFE_METHODS.has(method)) {
    headers.set('X-CSRFToken', getCookie('csrftoken'));
  }

  const res = await fetch(path, {
    ...init,
    method,
    headers,
    credentials: 'include',
  });

  if (res.status === 401) {
    const onLogin = window.location.pathname === '/login';
    if (!onLogin && !isLoginPath(path)) {
      window.location.href = '/login';
    }
    throw new ApiError(401, 'unauthenticated', 'Not authenticated.');
  }

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
    const errBody = body as ApiErrorBody | null;
    const code = errBody?.error?.code ?? 'error';
    const message =
      errBody?.error?.message ?? `Request failed (${res.status}).`;
    throw new ApiError(res.status, code, message);
  }

  return body as T;
}
