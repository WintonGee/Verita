import type { ReactNode } from 'react';
import type { UseQueryResult } from '@tanstack/react-query';
import { ApiError } from '../lib/apiClient';

interface QueryBoundaryProps<T> {
  query: UseQueryResult<T>;
  children: (data: T) => ReactNode;
  loadingLabel?: string;
}

/**
 * Wraps a TanStack query with uniform loading + error UI.
 * Empty states are the consumer's responsibility (they vary per surface),
 * so this only renders children once data is available.
 */
export function QueryBoundary<T>({
  query,
  children,
  loadingLabel = 'Loading…',
}: QueryBoundaryProps<T>) {
  if (query.isLoading) {
    return (
      <div className="py-8 text-center text-sm text-gray-500" role="status">
        {loadingLabel}
      </div>
    );
  }

  if (query.isError) {
    const err = query.error;
    const code = err instanceof ApiError ? err.code : 'error';
    const message =
      err instanceof Error ? err.message : 'Something went wrong.';
    return (
      <div
        className="my-4 rounded border border-red-300 bg-red-50 p-4 text-sm text-red-800"
        role="alert"
      >
        <div className="font-semibold">Couldn’t load this data.</div>
        <div className="mt-1 text-red-700">
          <span className="font-mono text-xs">{code}</span> — {message}
        </div>
        <button
          type="button"
          onClick={() => query.refetch()}
          className="mt-3 rounded border border-red-400 bg-white px-3 py-1 text-red-700 hover:bg-red-100"
        >
          Retry
        </button>
      </div>
    );
  }

  if (query.data === undefined) {
    return null;
  }

  return <>{children(query.data)}</>;
}
