import type { ReactNode } from 'react';
import { ApiError } from '../lib/apiClient';

interface QueryBoundaryProps<TData> {
  /** React Query's initial-fetch flag (v5: isPending). */
  isPending: boolean;
  isError: boolean;
  error: unknown;
  data: TData | undefined;
  /** Refetch callback for the error-state Retry button. */
  onRetry?: () => void;
  /** Optional predicate: when true, render the empty state instead of children. */
  isEmpty?: (data: TData) => boolean;
  /** Message shown in the empty state. */
  emptyMessage?: string;
  children: (data: TData) => ReactNode;
}

function Spinner() {
  return (
    <div
      role="status"
      aria-label="Loading"
      className="h-6 w-6 rounded-full border-2 border-gray-300 border-t-gray-700 animate-spin"
    />
  );
}

/**
 * Wraps a single React Query result and renders one of four states:
 * loading (spinner), error (red card with code + message + Retry),
 * empty ("nothing yet"), or the children with loaded data.
 */
export function QueryBoundary<TData>({
  isPending,
  isError,
  error,
  data,
  onRetry,
  isEmpty,
  emptyMessage = 'Nothing here yet.',
  children,
}: QueryBoundaryProps<TData>) {
  if (isPending) {
    return (
      <div className="flex items-center justify-center py-10">
        <Spinner />
      </div>
    );
  }

  if (isError || data === undefined) {
    const code = error instanceof ApiError ? error.code : 'unknown_error';
    const message =
      error instanceof ApiError
        ? error.message
        : error instanceof Error
          ? error.message
          : 'Something went wrong.';
    return (
      <div className="rounded-md border border-red-300 bg-red-50 p-4 text-sm">
        <p className="font-semibold text-red-800">Couldn&apos;t load this.</p>
        <p className="mt-1 text-red-700">
          <span className="font-mono text-xs">{code}</span>
          {' — '}
          {message}
        </p>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="mt-3 rounded bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-700"
          >
            Retry
          </button>
        )}
      </div>
    );
  }

  if (isEmpty && isEmpty(data)) {
    return (
      <div className="rounded-md border border-dashed border-gray-300 bg-gray-50 p-6 text-center text-sm text-gray-500">
        {emptyMessage}
      </div>
    );
  }

  return <>{children(data)}</>;
}
