import { useQuery } from '@tanstack/react-query';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { apiFetch } from '../lib/apiClient';
import { currentMonthStartISO, formatDayLabel, formatUnits } from '../lib/format';
import type { UsageResponse } from '../lib/types';
import { QueryBoundary } from './QueryBoundary';

export function UsageChart() {
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
        Usage this period (by day)
      </h2>
      <p className="mb-4 text-xs text-gray-500">
        Daily units consumed since the start of the current month.
      </p>
      <QueryBoundary
        isPending={query.isPending}
        isError={query.isError}
        error={query.error}
        data={query.data}
        onRetry={() => query.refetch()}
        isEmpty={(d) => d.data.length === 0}
        emptyMessage="No usage yet this period."
      >
        {(data) => {
          const chartData = data.data.map((w) => ({
            label: formatDayLabel(w.window_start),
            units: w.units_consumed,
          }));
          return (
            <div className="h-64 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={chartData}
                  margin={{ top: 8, right: 8, bottom: 8, left: 8 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
                  <XAxis
                    dataKey="label"
                    tick={{ fontSize: 11 }}
                    interval="preserveStartEnd"
                  />
                  <YAxis
                    tick={{ fontSize: 11 }}
                    width={56}
                    tickFormatter={(v: number) => formatUnits(v)}
                  />
                  <Tooltip
                    formatter={(v: number) => [formatUnits(v), 'Units']}
                  />
                  {/* Animation off: deterministic render (the enter animation
                      can jam at ~0 height under React 18 StrictMode double-mount,
                      leaving an empty chart). */}
                  <Bar dataKey="units" fill="#374151" isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          );
        }}
      </QueryBoundary>
    </section>
  );
}
