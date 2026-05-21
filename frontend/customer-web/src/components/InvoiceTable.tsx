import { useState } from 'react';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { apiFetch } from '../lib/apiClient';
import { formatDate, formatMoney } from '../lib/format';
import type { InvoiceListResponse } from '../lib/types';
import { QueryBoundary } from './QueryBoundary';
import { StatusBadge } from './StatusBadge';

const LIMIT = 25;

export function InvoiceTable() {
  const [page, setPage] = useState(1);

  const query = useQuery({
    queryKey: ['invoices', page, LIMIT],
    queryFn: () =>
      apiFetch<InvoiceListResponse>(`/v1/invoices?page=${page}&limit=${LIMIT}`),
    placeholderData: keepPreviousData,
  });

  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / LIMIT));

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <h2 className="mb-4 text-sm font-semibold text-gray-900">Invoices</h2>
      <QueryBoundary
        isPending={query.isPending}
        isError={query.isError}
        error={query.error}
        data={query.data}
        onRetry={() => query.refetch()}
        isEmpty={(d) => d.data.length === 0}
        emptyMessage="No invoices yet."
      >
        {(data) => (
          <div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
                    <th className="py-2 pr-4 font-medium">Period</th>
                    <th className="py-2 pr-4 font-medium">Status</th>
                    <th className="py-2 pr-4 text-right font-medium">Total</th>
                    <th className="py-2 pr-0 text-right font-medium">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.data.map((inv) => (
                    <tr
                      key={inv.id}
                      className="border-b border-gray-100 last:border-0"
                    >
                      <td className="py-2 pr-4 text-gray-900">
                        {formatDate(inv.period_start)} –{' '}
                        {formatDate(inv.period_end)}
                      </td>
                      <td className="py-2 pr-4">
                        <StatusBadge status={inv.status} />
                      </td>
                      <td className="py-2 pr-4 text-right font-mono text-gray-900">
                        {formatMoney(inv.total_micro_cents, inv.currency)}
                      </td>
                      <td className="py-2 pr-0 text-right">
                        <Link
                          to={`/invoices/${inv.id}`}
                          className="font-medium text-blue-600 hover:underline"
                        >
                          View
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mt-4 flex items-center justify-between text-sm">
              <span className="text-gray-500">
                Page {data.page} of {totalPages} · {total} total
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1 || query.isFetching}
                  className="rounded border border-gray-300 px-3 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
                >
                  Previous
                </button>
                <button
                  type="button"
                  onClick={() => setPage((p) => p + 1)}
                  disabled={page >= totalPages || query.isFetching}
                  className="rounded border border-gray-300 px-3 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
                >
                  Next
                </button>
              </div>
            </div>
          </div>
        )}
      </QueryBoundary>
    </section>
  );
}
