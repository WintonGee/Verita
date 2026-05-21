import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './apiClient';
import type { LoginResponse, MeResponse } from '../types';

export const ME_QUERY_KEY = ['me'] as const;

/**
 * GET /ops/auth/me — bootstraps auth state on app load. This view also sets the
 * csrftoken cookie, so it must run before any mutation. retry:false so an
 * unauthenticated user resolves to an error quickly (no redirect churn).
 */
export function useMe() {
  return useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: () => apiFetch<MeResponse>('/ops/auth/me'),
    retry: false,
    staleTime: Infinity,
  });
}

export function login(username: string, password: string) {
  return apiFetch<LoginResponse>('/ops/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

export function logout() {
  return apiFetch<void>('/ops/auth/logout', { method: 'POST' });
}
