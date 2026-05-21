import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '../lib/apiClient';
import type { MeResponse } from '../lib/types';

export const ME_QUERY_KEY = ['me'] as const;

/**
 * Auth state is derived entirely from the /v1/me query.
 * - staleTime: Infinity — the session doesn't change under us; we invalidate
 *   explicitly on login (setQueryData) and logout (queryClient.clear()).
 * - retry: false — a 401 means "not logged in"; retrying won't help.
 */
export function useMe() {
  return useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: () => apiFetch<MeResponse>('/v1/me'),
    staleTime: Infinity,
    retry: false,
  });
}
