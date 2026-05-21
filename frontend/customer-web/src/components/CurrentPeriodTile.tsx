import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '../lib/apiClient';
import { currentMonthStartISO, formatUnits } from '../lib/format';
import type { UsageResponse } from '../lib/types';
import { QueryBoundary } from './QueryBoundary';

/**
 * "Current period" summary tile: total units consumed since the start of the
 * current calendar month. Reuses the same daily usage query as the chart
 * (identical queryKey → shared cache, single network request).
 */
export function CurrentPeriodTile() {
  const start = currentMonthStartISO();

  const query = useQuery({
    queryKey: ['usage', 'day', start],
    queryFn: () =>
      apiFetch<UsageResponse>(
        `/v1/usage?granularity=day&start=${encodeURIComponent(start)}&limit=100`,
      ),
  });

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <h2 className="mb-1 text-sm font-semibold text-gray-900">
        Current period
      </h2>
      <p className="mb-4 text-xs text-gray-500">
        Total units consumed this month.
      </p>
      <QueryBoundary
        isPending={query.isPending}
        isError={query.isError}
        error={query.error}
        data={query.data}
        onRetry={() => query.refetch()}
      >
        {(data) => {
          // Integer unit counts; safe to sum.
          const totalUnits = data.data.reduce(
            (acc, w) => acc + w.units_consumed,
            0,
          );
          return (
            <div>
              <p className="text-3xl font-bold text-gray-900">
                {formatUnits(totalUnits)}
              </p>
              <p className="mt-1 text-xs text-gray-500">units consumed</p>
            </div>
          );
        }}
      </QueryBoundary>
    </section>
  );
}
